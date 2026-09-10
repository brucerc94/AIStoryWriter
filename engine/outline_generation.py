"""Two-stage Generate Outline pipeline.

Generate Outline uses a stateless pipeline:
1) partition the story draft into semantic chapter source blocks;
2) prepare the base Characters/World once from that story material;
3) for each chapter, select only relevant Characters/World;
4) generate a rich narrative treatment for that chapter in a fresh inference;
5) if the treatment is truncated, continue the same chapter with another fresh inference;
6) move to the next chapter only after the current chapter is complete.

The chapter treatment is intentionally richer than a checklist. It is the
material the Write Chapter workflow will later turn into polished novel prose.
"""

from __future__ import annotations

import json
import logging
import re

from engine.models import TaskType
from engine.context import build_relevant_chapter_context
from engine import prompts, storage

logger = logging.getLogger("workflow")

_MAX_SPLIT_TOKENS = 3500
_MAX_OUTLINE_PASSES = 8
_MIN_SOURCE_CHARS = 200
_MIN_TREATMENT_CHARS = 500
_MAX_CHARACTER_CHARS = 4200
_MAX_WORLD_CHARS = 5000
_CONTINUATION_CHECKPOINT_CHARS = 2400


# ---------------------------------------------------------------------------
# JSON parsing — tolerant of imperfect local-model output
# ---------------------------------------------------------------------------

def _clean_json_for_local_models(text: str) -> str:
    """Apply lightweight fixes that make imperfect local-model JSON parseable.

    Fixes applied (in order, each idempotent):
    1. Remove trailing commas before ] or } (invalid JSON, common LLM mistake).
    2. Strip a leading BOM or stray whitespace.
    """
    # Remove trailing commas before closing brackets/braces
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text.strip()


def _parse_json_object(raw: str) -> tuple[dict | None, str]:
    """Parse the first JSON object found in *raw*.

    Returns (parsed_dict, reason_string). On success reason is 'ok'.
    On failure reason describes exactly what went wrong.
    """
    text = (raw or "").strip()
    if not text:
        return None, "empty input"

    start = text.find("{")
    end = text.rfind("}")
    if start < 0:
        return None, "no opening brace '{' found in model output"
    if end <= start:
        return None, "no closing brace '}' found after opening brace"

    candidate = text[start:end + 1]

    # First attempt: strict parse
    try:
        value = json.loads(candidate)
        if not isinstance(value, dict):
            return None, f"JSON parsed but top-level type is {type(value).__name__!r}, expected object"
        return value, "ok"
    except json.JSONDecodeError as e:
        first_error = str(e)

    # Second attempt: apply local-model tolerances
    cleaned = _clean_json_for_local_models(candidate)
    try:
        value = json.loads(cleaned)
        if not isinstance(value, dict):
            return None, f"JSON (after cleanup) parsed but top-level type is {type(value).__name__!r}"
        logger.info("[generate_outline] Stage 1 JSON parsed after cleanup (trailing-comma fix applied).")
        return value, "ok"
    except json.JSONDecodeError as e:
        return None, f"JSONDecodeError (original: {first_error!r}; after cleanup: {e!r})"


def _extract_chapter_blocks(raw: str, requested_count: int) -> list[str]:
    """Extract ordered source blocks from Stage 1 JSON output.

    Returns an empty list on any validation failure. All failures are logged
    with the specific reason so the operator can diagnose the problem.
    """
    data, reason = _parse_json_object(raw)
    if data is None:
        logger.error(
            "[generate_outline] Stage 1 JSON parse failed — %s. "
            "Raw output (first 500 chars): %r",
            reason,
            (raw or "")[:500],
        )
        return []

    chapters = data.get("chapters")
    if not isinstance(chapters, list):
        logger.error(
            "[generate_outline] Stage 1 JSON has no 'chapters' list. "
            "Top-level keys: %s",
            list(data.keys()),
        )
        return []

    if len(chapters) == 0:
        logger.error(
            "[generate_outline] Stage 1 'chapters' list is empty (0 items)."
        )
        return []

    blocks: list[tuple[int, str]] = []
    seen: set[int] = set()
    skipped: list[str] = []

    for i, entry in enumerate(chapters):
        if not isinstance(entry, dict):
            skipped.append(f"item[{i}]: not a dict (got {type(entry).__name__!r})")
            continue

        raw_number = entry.get("number")
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            skipped.append(f"item[{i}]: 'number' is {raw_number!r} (not an integer)")
            continue

        if number < 1 or number > requested_count:
            skipped.append(
                f"item[{i}]: chapter number {number} out of range 1–{requested_count}"
            )
            continue

        if number in seen:
            skipped.append(f"item[{i}]: duplicate chapter number {number}")
            continue

        source = str(entry.get("source", "")).strip()
        if not source:
            skipped.append(f"item[{i}]: chapter {number} has empty 'source'")
            continue

        seen.add(number)
        blocks.append((number, source))

    if skipped:
        logger.warning(
            "[generate_outline] Stage 1 skipped %d chapter entry/entries: %s",
            len(skipped),
            "; ".join(skipped),
        )

    blocks.sort(key=lambda item: item[0])
    present_numbers = [n for n, _ in blocks]
    expected_numbers = list(range(1, requested_count + 1))

    if present_numbers != expected_numbers:
        missing = sorted(set(expected_numbers) - set(present_numbers))
        extra = sorted(set(present_numbers) - set(expected_numbers))
        logger.error(
            "[generate_outline] Stage 1 chapter numbering mismatch. "
            "Expected %s, got %s. Missing: %s. Extra/out-of-range: %s.",
            expected_numbers,
            present_numbers,
            missing,
            extra,
        )
        return []

    logger.info(
        "[generate_outline] Stage 1 extracted %d/%d chapter block(s) successfully.",
        len(blocks),
        requested_count,
    )
    return [source for _, source in blocks]


# ---------------------------------------------------------------------------
# Outline normalisation helpers
# ---------------------------------------------------------------------------

def _normalize_outline_entry(text: str, chapter_num: int) -> str:
    text = (text or "").strip()
    if not text:
        return ""

    fenced = re.match(r"^\s*```(?:[a-zA-Z]*)\n(.*)\n```\s*$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    heading = re.search(
        rf"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+{chapter_num}\b[^\n]*$",
        text,
    )
    if heading:
        text = text[heading.start():].strip()
    return text


def _strip_duplicate_heading(text: str, chapter_num: int) -> str:
    text = (text or "").strip()
    return re.sub(
        rf"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+{chapter_num}\b[^\n]*\s*",
        "",
        text,
        count=1,
    ).strip()


def _treatment_complete(text: str, chapter_num: int) -> bool:
    normalized = _normalize_outline_entry(text, chapter_num)
    if not normalized:
        return False
    if not re.search(
        rf"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+{chapter_num}\b",
        normalized,
    ):
        return False

    body = _strip_duplicate_heading(normalized, chapter_num).strip()
    if len(body) < _MIN_TREATMENT_CHARS:
        return False

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    if len(paragraphs) < 2:
        return False

    return bool(re.search(r'[.!?"\')\]]\s*$', body))


def _previous_chapter_context(entry: str) -> str:
    text = _strip_duplicate_heading(entry or "", 0).strip()
    if not text:
        return "(none — this is the first chapter)"

    if len(text) > _CONTINUATION_CHECKPOINT_CHARS:
        return text[-_CONTINUATION_CHECKPOINT_CHARS:].lstrip()
    return text


def _continuation_checkpoint(partial: str) -> str:
    text = (partial or "").strip()
    if len(text) > _CONTINUATION_CHECKPOINT_CHARS:
        return text[-_CONTINUATION_CHECKPOINT_CHARS:].lstrip()
    return text


def _sanitize_continuation_addition(addition: str, chapter_num: int) -> str:
    """Remove wrapper headings without changing the narrative content."""
    text = (addition or "").strip()
    if not text:
        return ""

    fenced = re.match(r"^\s*```(?:[a-zA-Z]*)\n(.*)\n```\s*$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    text = re.sub(
        rf"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+{chapter_num}\b[^\n]*\s*",
        "",
        text,
        count=1,
    )
    text = re.sub(r"(?im)^\s*(?:Chapter Plan|Narrative Treatment|Continuity)\s*:\s*$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Story source construction
# ---------------------------------------------------------------------------

def _story_source(worker, requested_count: int) -> str:
    synopsis = (worker.project.synopsis or "").strip()
    request = (worker.extra_input or "").strip()
    parts: list[str] = []
    if synopsis:
        parts.append(f"STORY DRAFT / SYNOPSIS:\n{synopsis}")
    if request:
        parts.append(f"AUTHOR STORY INPUT:\n{request}")
    parts.append(f"REQUESTED CHAPTER COUNT: {requested_count}")
    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Stage 1: semantic split
# ---------------------------------------------------------------------------

def _split_story(worker, requested_count: int, story_source: str) -> list[str]:
    if len(story_source.strip()) < _MIN_SOURCE_CHARS:
        logger.warning("[generate_outline] story source is too short for semantic chapter splitting.")
        return []

    system = prompts.render("outline/split_system", requested_count=requested_count)
    user = prompts.render(
        "outline/split_user",
        requested_count=requested_count,
        story_source=story_source,
    )
    logger.info(
        "[generate_outline] Stage 1: semantic split into %d chapter source block(s).",
        requested_count,
    )
    raw = worker._run_lean_inference(
        TaskType.GENERATE_OUTLINE,
        system,
        user,
        max_tokens=_MAX_SPLIT_TOKENS,
    )
    logger.info(
        "[generate_outline] Stage 1 raw output: %d tokens-equivalent, %d chars. Preview: %r",
        len(raw.split()),
        len(raw),
        raw[:300],
    )
    blocks = _extract_chapter_blocks(raw, requested_count)
    if not blocks:
        logger.error(
            "[generate_outline] Stage 1 failed to produce a valid partition "
            "for %d chapter(s). Full raw output:\n%s",
            requested_count,
            raw,
        )
        return []
    logger.info("[generate_outline] Stage 1 complete: %d block(s).", len(blocks))
    return blocks


# ---------------------------------------------------------------------------
# Canon preparation and selection
# ---------------------------------------------------------------------------

def _prepare_base_canon(worker, story_source: str) -> None:
    """Build the established Characters/World base once from the story draft."""
    logger.info("[generate_outline] Preparing base Characters/World from story draft.")
    if story_source.strip():
        worker._extract_and_merge_characters(story_source)
        worker._update_world_incremental(story_source, source_type="outline_source")


def _select_relevant_canon(worker, source_block: str, previous_entry: str) -> tuple[str, str]:
    selection_source = source_block.strip()
    previous = _previous_chapter_context(previous_entry)
    if previous and not previous.startswith("(none"):
        selection_source += f"\n\nPREVIOUS CHAPTER CONTEXT:\n{previous}"

    characters, world = build_relevant_chapter_context(
        worker.project,
        selection_source,
        max_character_chars=_MAX_CHARACTER_CHARS,
        max_world_chars=_MAX_WORLD_CHARS,
    )
    logger.info(
        "[generate_outline] Selected canon for chapter: characters=%d chars, world=%d chars.",
        len(characters),
        len(world),
    )
    return characters or "(none relevant)", world or "(none relevant)"


# ---------------------------------------------------------------------------
# Author profile — full Creative Intent + Writing Style
# ---------------------------------------------------------------------------

def _build_author_profile(worker) -> str:
    """Return the full author profile: Creative Intent + Writing Style.

    Both sections must be present so that every chapter-treatment inference
    receives the author's complete creative vision, not just formatting
    preferences.
    """
    intent = worker.project.author_intent.to_prompt_fragment().strip()
    style = worker.project.writing_style.to_prompt_fragment().strip()

    parts: list[str] = []
    if intent:
        parts.append(f"CREATIVE INTENT:\n{intent}")
    if style:
        parts.append(f"WRITING STYLE:\n{style}")
    return "\n\n".join(parts).strip() or "(none specified)"


# ---------------------------------------------------------------------------
# Stage 2: chapter prompt construction
# ---------------------------------------------------------------------------

def _build_chapter_prompt(
    worker,
    chapter_num: int,
    source_block: str,
    previous_entry: str,
    characters: str,
    world: str,
) -> tuple[str, str]:
    language = worker._response_language()
    language_note = f" Write in {language}." if language else ""
    author_profile = _build_author_profile(worker)
    previous = _previous_chapter_context(previous_entry)

    system = prompts.render("outline/chapter_system", language_note=language_note)
    user = prompts.render(
        "outline/chapter_user",
        chapter_number=chapter_num,
        source_block=source_block,
        author_profile=author_profile,
        characters=characters,
        world=world,
        previous_continuity=previous,
    )
    return system, user


def _build_continuation_prompt(
    worker,
    chapter_num: int,
    source_block: str,
    partial: str,
    previous_entry: str,
    characters: str,
    world: str,
) -> tuple[str, str]:
    language = worker._response_language()
    language_note = f" Write in {language}." if language else ""
    author_profile = _build_author_profile(worker)
    previous = _previous_chapter_context(previous_entry)
    checkpoint = _continuation_checkpoint(partial)

    system = prompts.render("outline/chapter_system", language_note=language_note)
    user = prompts.render(
        "outline/chapter_continue_user",
        chapter_number=chapter_num,
        source_block=source_block,
        author_profile=author_profile,
        characters=characters,
        world=world,
        previous_continuity=previous,
        partial_checkpoint=checkpoint,
    )
    return system, user


# ---------------------------------------------------------------------------
# Stage 2: chapter treatment generation with continuation
# ---------------------------------------------------------------------------

def _write_chapter_outline(worker, chapter_num: int, source_block: str, previous_entry: str) -> str:
    characters, world = _select_relevant_canon(worker, source_block, previous_entry)
    system, user = _build_chapter_prompt(
        worker, chapter_num, source_block, previous_entry, characters, world
    )

    partial = worker._run_lean_inference(
        TaskType.GENERATE_OUTLINE,
        system,
        user,
        max_tokens=worker._content_max_tokens(),
    ).strip()
    if not partial:
        logger.error("[generate_outline] Chapter %d initial inference returned empty output.", chapter_num)
        return ""

    partial = _normalize_outline_entry(partial, chapter_num)

    for continuation_pass in range(1, _MAX_OUTLINE_PASSES + 1):
        if _treatment_complete(partial, chapter_num):
            logger.info(
                "[generate_outline] Chapter %d treatment complete after %d inference(s).",
                chapter_num,
                continuation_pass,
            )
            return partial

        checkpoint = _continuation_checkpoint(partial)
        logger.info(
            "[generate_outline] Chapter %d treatment incomplete; continuation %d/%d "
            "(partial=%d chars, checkpoint=%d chars).",
            chapter_num,
            continuation_pass,
            _MAX_OUTLINE_PASSES,
            len(partial),
            len(checkpoint),
        )

        system, user = _build_continuation_prompt(
            worker,
            chapter_num,
            source_block,
            partial,
            previous_entry,
            characters,
            world,
        )
        addition = worker._run_lean_inference(
            TaskType.GENERATE_OUTLINE,
            system,
            user,
            max_tokens=worker._content_max_tokens(),
        ).strip()
        addition = _sanitize_continuation_addition(addition, chapter_num)
        if not addition:
            logger.warning(
                "[generate_outline] Chapter %d continuation %d produced no new content; stopping.",
                chapter_num,
                continuation_pass,
            )
            break

        partial = (partial.rstrip() + "\n\n" + addition).strip()
        partial = _normalize_outline_entry(partial, chapter_num)

    if _treatment_complete(partial, chapter_num):
        return partial

    logger.warning(
        "[generate_outline] Chapter %d remained incomplete after %d inference(s). "
        "Treatment length: %d chars.",
        chapter_num,
        _MAX_OUTLINE_PASSES + 1,
        len(partial),
    )
    return ""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_generate_outline(worker) -> None:
    requested_count = worker._extract_requested_chapter_count(worker.extra_input)
    if requested_count is None or requested_count < 1:
        worker.error_occurred.emit("Generate Outline requires a valid requested chapter count.")
        return

    story_source = _story_source(worker, requested_count)
    if not story_source:
        worker.error_occurred.emit("Generate Outline requires story material.")
        return

    worker.step_started.emit(
        f"Dividing story draft into {requested_count} chapter source blocks..."
    )
    source_blocks = _split_story(worker, requested_count, story_source)
    if len(source_blocks) != requested_count:
        worker.error_occurred.emit(
            "Could not reliably divide the story draft into the requested number of chapters. "
            "Nothing was saved — try again."
        )
        return

    _prepare_base_canon(worker, story_source)

    generated_entries: list[str] = []
    previous_entry = ""
    for chapter_num, source_block in enumerate(source_blocks, start=1):
        if worker._cancelled:
            return
        worker.step_started.emit(
            f"Writing outline for Chapter {chapter_num}/{requested_count}..."
        )
        logger.info(
            "[generate_outline] RESET: starting Chapter %d/%d with its source block + "
            "relevant canon + full Author Profile (Creative Intent + Writing Style).",
            chapter_num,
            requested_count,
        )
        entry = _write_chapter_outline(worker, chapter_num, source_block, previous_entry)
        if not entry:
            worker.error_occurred.emit(
                f"Could not complete the outline for Chapter {chapter_num}. "
                "Nothing was saved — try again."
            )
            return
        generated_entries.append(entry)
        previous_entry = entry
        logger.info(
            "[generate_outline] Chapter %d saved in memory; moving to next chapter.",
            chapter_num,
        )

    outline_text = "\n\n".join(generated_entries).strip()
    worker.project.outline = outline_text
    storage.save_project(worker.project)
    worker.step_finished.emit("Outline", outline_text)
    logger.info(
        "[generate_outline] Generate Outline complete: %d chapter(s), rich treatment pipeline.",
        requested_count,
    )


def install(module) -> None:
    if getattr(module, "_outline_generation_installed", False):
        return
    worker_cls = getattr(module, "WorkflowWorker", None)
    if worker_cls is None:
        raise RuntimeError("Generate Outline hook could not find WorkflowWorker.")
    if getattr(worker_cls, "_outline_generation_installed", False):
        return
    worker_cls._run_generate_outline = run_generate_outline
    worker_cls._outline_generation_installed = True
    module._outline_generation_installed = True
    logger.info(
        "[generate_outline] Rich chapter-treatment pipeline installed on WorkflowWorker."
    )