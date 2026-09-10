"""Generate Outline and Extend Outline pipelines.

Both flows use the same chapter-treatment architecture:
source material -> semantic chapter chunks -> relevant canon selection ->
full Author Profile -> rich narrative treatment -> bounded continuation.

Neither flow creates or updates Characters or World.
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


def _clean_json_for_local_models(text: str) -> str:
    """Repair small, safe JSON mistakes commonly produced by local models."""
    text = re.sub(r'}\s*(?=\{"number"\s*:)', '},', text)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text.strip()


def _parse_json_object(raw: str) -> tuple[dict | None, str]:
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

    try:
        value = json.loads(candidate)
        if not isinstance(value, dict):
            return None, (
                f"JSON parsed but top-level type is {type(value).__name__!r}, "
                "expected object"
            )
        return value, "ok"
    except json.JSONDecodeError as first_error:
        first_message = str(first_error)

    cleaned = _clean_json_for_local_models(candidate)
    try:
        value = json.loads(cleaned)
        if not isinstance(value, dict):
            return None, (
                f"JSON after cleanup parsed but top-level type is "
                f"{type(value).__name__!r}"
            )
        logger.info(
            "[generate_outline] Stage 1 JSON parsed after local-model cleanup."
        )
        return value, "ok"
    except json.JSONDecodeError as second_error:
        return None, (
            "JSONDecodeError "
            f"(original: {first_message!r}; after cleanup: {str(second_error)!r})"
        )


def _extract_chapter_blocks(raw: str, requested_count: int) -> list[str]:
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

    blocks: list[tuple[int, str]] = []
    seen: set[int] = set()
    skipped: list[str] = []

    for i, entry in enumerate(chapters):
        if not isinstance(entry, dict):
            skipped.append(f"item[{i}]: not a dict")
            continue
        try:
            number = int(entry.get("number"))
        except (TypeError, ValueError):
            skipped.append(
                f"item[{i}]: invalid chapter number {entry.get('number')!r}"
            )
            continue
        if number < 1 or number > requested_count:
            skipped.append(
                f"item[{i}]: chapter number {number} outside 1..{requested_count}"
            )
            continue
        if number in seen:
            skipped.append(f"item[{i}]: duplicate chapter number {number}")
            continue
        source = str(entry.get("source", "")).strip()
        if not source:
            skipped.append(f"item[{i}]: chapter {number} has empty source")
            continue
        seen.add(number)
        blocks.append((number, source))

    if skipped:
        logger.warning(
            "[generate_outline] Stage 1 skipped entries: %s",
            "; ".join(skipped),
        )

    blocks.sort(key=lambda item: item[0])
    numbers = [number for number, _ in blocks]
    expected = list(range(1, requested_count + 1))
    if numbers != expected:
        missing = sorted(set(expected) - set(numbers))
        extra = sorted(set(numbers) - set(expected))
        logger.error(
            "[generate_outline] Stage 1 numbering mismatch. "
            "Expected %s, got %s. Missing=%s Extra=%s.",
            expected,
            numbers,
            missing,
            extra,
        )
        return []

    return [source for _, source in blocks]


def _normalize_outline_entry(text: str, chapter_num: int) -> str:
    text = (text or "").strip()
    if not text:
        return ""

    fenced = re.match(
        r"^\s*```(?:[a-zA-Z]*)\n(.*)\n```\s*$",
        text,
        re.DOTALL,
    )
    if fenced:
        text = fenced.group(1).strip()

    heading = re.search(
        rf"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+{chapter_num}\b[^\n]*$",
        text,
    )
    if heading:
        text = text[heading.start():].strip()

    return _truncate_after_target_chapter(text, chapter_num)


def _truncate_after_target_chapter(text: str, chapter_num: int) -> str:
    pattern = re.compile(
        r"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+(\d+)\b[^\n]*$"
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return text.strip()

    target_index = next(
        (i for i, match in enumerate(matches) if int(match.group(1)) == chapter_num),
        None,
    )
    if target_index is None:
        return text.strip()

    start = matches[target_index].start()
    end = (
        matches[target_index + 1].start()
        if target_index + 1 < len(matches)
        else len(text)
    )
    return text[start:end].strip()


def _strip_duplicate_heading(text: str, chapter_num: int) -> str:
    return re.sub(
        rf"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+{chapter_num}\b[^\n]*\s*",
        "",
        (text or "").strip(),
        count=1,
    ).strip()


def _treatment_complete(text: str, chapter_num: int) -> bool:
    normalized = _normalize_outline_entry(text, chapter_num)
    if not normalized:
        return False
    body = _strip_duplicate_heading(normalized, chapter_num)
    if len(body) < _MIN_TREATMENT_CHARS:
        return False
    paragraphs = [
        part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()
    ]
    if len(paragraphs) < 2:
        return False
    return bool(re.search(r'[.!?"\')\]]\s*$', body))


def _previous_chapter_context(entry: str) -> str:
    text = (entry or "").strip()
    if not text:
        return "(none — this is the first chapter)"
    return text[-_CONTINUATION_CHECKPOINT_CHARS:].lstrip()


def _continuation_checkpoint(partial: str) -> str:
    text = (partial or "").strip()
    return text[-_CONTINUATION_CHECKPOINT_CHARS:].lstrip()


def _sanitize_continuation_addition(addition: str, chapter_num: int) -> str:
    text = (addition or "").strip()
    if not text:
        return ""

    fenced = re.match(
        r"^\s*```(?:[a-zA-Z]*)\n(.*)\n```\s*$",
        text,
        re.DOTALL,
    )
    if fenced:
        text = fenced.group(1).strip()

    text = re.sub(
        rf"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+{chapter_num}\b[^\n]*\s*",
        "",
        text,
        count=1,
    )
    next_heading = re.search(
        rf"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+{chapter_num + 1}\b[^\n]*$",
        text,
    )
    if next_heading:
        text = text[:next_heading.start()].rstrip()

    text = re.sub(
        r"(?im)^\s*(?:Chapter Plan|Narrative Treatment|Continuity)\s*:\s*$",
        "",
        text,
    )
    return text.strip()


def _build_author_profile(worker) -> str:
    intent = worker.project.author_intent.to_prompt_fragment().strip()
    style = worker.project.writing_style.to_prompt_fragment().strip()
    parts: list[str] = []
    if intent:
        parts.append(f"CREATIVE INTENT:\n{intent}")
    if style:
        parts.append(f"WRITING STYLE:\n{style}")
    return "\n\n".join(parts).strip() or "(none specified)"


def _select_relevant_canon(worker, source_block: str) -> tuple[str, str]:
    characters, world = build_relevant_chapter_context(
        worker.project,
        source_block.strip(),
        max_character_chars=_MAX_CHARACTER_CHARS,
        max_world_chars=_MAX_WORLD_CHARS,
    )
    logger.info(
        "[generate_outline] Relevant canon from current chunk: characters=%d chars, world=%d chars.",
        len(characters),
        len(world),
    )
    return characters or "(none relevant)", world or "(none relevant)"


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
    system = prompts.render(
        "outline/chapter_system",
        language_note=language_note,
    )
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
    system = prompts.render(
        "outline/chapter_system",
        language_note=language_note,
    )
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


def _write_chapter_outline(
    worker,
    chapter_num: int,
    source_block: str,
    previous_entry: str,
) -> str:
    characters, world = _select_relevant_canon(worker, source_block)
    system, user = _build_chapter_prompt(
        worker,
        chapter_num,
        source_block,
        previous_entry,
        characters,
        world,
    )
    partial = worker._run_lean_inference(
        TaskType.GENERATE_OUTLINE,
        system,
        user,
        max_tokens=worker._content_max_tokens(),
    ).strip()
    if not partial:
        logger.error("[generate_outline] Chapter %d returned empty output.", chapter_num)
        return ""

    partial = _normalize_outline_entry(partial, chapter_num)
    for _ in range(1, _MAX_OUTLINE_PASSES + 1):
        if _treatment_complete(partial, chapter_num):
            return partial

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
            break

        partial = (partial.rstrip() + "\n\n" + addition).strip()
        partial = _normalize_outline_entry(partial, chapter_num)

    return partial if _treatment_complete(partial, chapter_num) else ""


def _story_source(worker, requested_count: int, source_text: str | None = None) -> str:
    source = (
        source_text if source_text is not None else (worker.project.synopsis or "")
    ).strip()
    request = (worker.extra_input or "").strip() if source_text is None else ""
    parts: list[str] = []
    if source:
        parts.append(f"STORY DRAFT / SYNOPSIS:\n{source}")
    if request:
        parts.append(f"AUTHOR STORY INPUT:\n{request}")
    parts.append(f"REQUESTED CHAPTER COUNT: {requested_count}")
    return "\n\n".join(parts).strip()


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
            "[generate_outline] Stage 1 failed to produce a valid partition for %d chapter(s).",
            requested_count,
        )
        return []
    logger.info("[generate_outline] Stage 1 complete: %d block(s).", len(blocks))
    return blocks


def _extract_requested_chapter_count(worker) -> int | None:
    match = re.search(
        r"EXACTLY\s+(\d+)\s+chapters",
        worker.extra_input or "",
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def run_generate_outline(worker) -> None:
    if worker.extra_input.startswith(OUTLINE_EXTEND_MARKER):
        return _run_extend_outline(worker)

    if worker.extra_input.startswith(OUTLINE_SUGGESTION_MARKER):
        handler = getattr(worker, "_run_regenerate_outline_with_suggestion", None)
        if handler is None:
            worker.error_occurred.emit("Outline suggestion handler is unavailable.")
            return
        return handler()

    requested_count = _extract_requested_chapter_count(worker)
    if requested_count is None or requested_count < 1:
        worker.error_occurred.emit("Generate Outline requires a valid requested chapter count.")
        return

    synopsis = (worker.project.synopsis or "").strip()
    if not synopsis:
        worker.error_occurred.emit("Generate Outline requires story material.")
        return

    story_source = _story_source(worker, requested_count, synopsis)
    worker.step_started.emit(
        f"Dividing story draft into {requested_count} chapter source blocks..."
    )
    source_blocks = _split_story(worker, requested_count, story_source)
    if len(source_blocks) != requested_count:
        worker.error_occurred.emit(
            "Could not reliably divide the story draft into the requested number "
            "of chapters. Nothing was saved — try again."
        )
        return

    generated_entries: list[str] = []
    previous_entry = ""
    for chapter_num, source_block in enumerate(source_blocks, start=1):
        if worker._cancelled:
            return
        worker.step_started.emit(
            f"Writing outline for Chapter {chapter_num}/{requested_count}..."
        )
        logger.info(
            "[generate_outline] Chapter %d/%d: selecting relevant Characters/World "
            "from chunk, then applying full Author Profile.",
            chapter_num,
            requested_count,
        )
        entry = _write_chapter_outline(
            worker,
            chapter_num,
            source_block,
            previous_entry,
        )
        if not entry:
            worker.error_occurred.emit(
                f"Could not complete the outline for Chapter {chapter_num}. "
                "Nothing was saved — try again."
            )
            return
        generated_entries.append(entry)
        previous_entry = entry

    outline_text = "\n\n".join(generated_entries).strip()
    worker.project.outline = outline_text
    storage.save_project(worker.project)
    worker.step_finished.emit("Outline", outline_text)
    logger.info(
        "[generate_outline] Generate Outline complete: %d chapter(s). "
        "No Character/World creation or update performed.",
        requested_count,
    )


def _parse_extend_payload(worker) -> tuple[int, str] | None:
    payload = worker.extra_input.split(OUTLINE_EXTEND_MARKER, 1)[1].strip("\n")
    count_line, _, request = payload.partition("\n")
    try:
        count = int(count_line.strip())
    except ValueError:
        return None
    if count < 1:
        return None
    request = request.strip()
    if not request:
        return None
    return count, request


def _last_outline_entry(outline: str) -> str:
    text = (outline or "").strip()
    if not text:
        return ""
    matches = list(
        re.finditer(
            r"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+(\d+)\b[^\n]*$",
            text,
        )
    )
    if not matches:
        return text[-_CONTINUATION_CHECKPOINT_CHARS:].strip()
    return text[matches[-1].start():].strip()


def _run_extend_outline(worker) -> None:
    parsed = _parse_extend_payload(worker)
    if parsed is None:
        worker.error_occurred.emit(
            "Extend Outline requires a valid chapter count and story material."
        )
        return

    requested_count, user_request = parsed
    current_outline = (worker.project.outline or "").strip()
    if not current_outline:
        worker.error_occurred.emit(
            "There is no outline to extend. Generate an outline first."
        )
        return

    next_chapter = max(
        (
            int(match.group(1))
            for match in re.finditer(
                r"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+(\d+)\b",
                current_outline,
            )
        ),
        default=0,
    ) + 1

    split_source = (
        f"STORY MATERIAL TO EXTEND:\n{user_request}\n\n"
        f"REQUESTED NEW CHAPTER COUNT: {requested_count}"
    )
    worker.step_started.emit(
        f"Dividing extension request into {requested_count} chapter source blocks..."
    )
    source_blocks = _split_story(worker, requested_count, split_source)
    if len(source_blocks) != requested_count:
        worker.error_occurred.emit(
            "Could not reliably divide the extension request into the requested "
            "number of chapters. Nothing was appended — try again."
        )
        return

    generated_entries: list[str] = []
    previous_entry = _last_outline_entry(current_outline)

    for index, source_block in enumerate(source_blocks):
        if worker._cancelled:
            return
        chapter_num = next_chapter + index
        worker.step_started.emit(
            f"Writing outline for Chapter {chapter_num}/{next_chapter + requested_count - 1}..."
        )
        logger.info(
            "[extend_outline] Chapter %d: selecting relevant Characters/World from "
            "extension chunk only, then applying full Author Profile.",
            chapter_num,
        )
        entry = _write_chapter_outline(
            worker,
            chapter_num,
            source_block,
            previous_entry,
        )
        if not entry:
            worker.error_occurred.emit(
                f"Could not complete the outline for Chapter {chapter_num}. "
                "Nothing was appended — try again."
            )
            return
        generated_entries.append(entry)
        previous_entry = entry

    new_outline = "\n\n".join(generated_entries).strip()
    if len(generated_entries) != requested_count:
        worker.error_occurred.emit(
            f"Expected {requested_count} new chapters but generated {len(generated_entries)}."
        )
        return

    worker.project.outline = (
        current_outline.rstrip() + "\n\n" + new_outline
    ).strip()
    storage.save_project(worker.project)
    worker.step_finished.emit(
        f"Outline Extended (Chapters {next_chapter}-{next_chapter + requested_count - 1})",
        new_outline,
    )
    logger.info(
        "[extend_outline] Extend Outline complete: %d chapter(s). "
        "No Character/World creation or update performed.",
        requested_count,
    )


def install(module) -> None:
    if getattr(module, "_outline_generation_installed", False):
        return
    worker_cls = getattr(module, "WorkflowWorker", None)
    if worker_cls is None:
        raise RuntimeError("Outline generation hook could not find WorkflowWorker.")
    worker_cls._run_generate_outline = run_generate_outline
    worker_cls._run_extend_outline = _run_extend_outline
    worker_cls._outline_generation_installed = True
    module._outline_generation_installed = True
    logger.info(
        "[generate_outline] Shared rich Generate/Extend Outline pipeline installed."
    )
