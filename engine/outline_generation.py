"""Two-stage Generate Outline pipeline.

Stage 1 partitions the complete story draft into semantic chapter source blocks.
Stage 2 writes one detailed outline entry per chapter from only that chapter's
source block plus compact established canon/style context. Every inference is
lean and stateless: no chat history is used between stages or chapter passes.
"""

from __future__ import annotations

import json
import logging
import re

from engine.models import TaskType
from engine.context import format_characters_block
from engine import prompts, storage

logger = logging.getLogger("workflow")

_MAX_SPLIT_TOKENS = 3500
_MAX_OUTLINE_PASSES = 8
_MIN_SOURCE_CHARS = 200


def _parse_json_object(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _extract_chapter_blocks(raw: str, requested_count: int) -> list[str]:
    data = _parse_json_object(raw)
    if not data:
        return []
    chapters = data.get("chapters")
    if not isinstance(chapters, list):
        return []

    blocks: list[tuple[int, str]] = []
    seen: set[int] = set()
    for entry in chapters:
        if not isinstance(entry, dict):
            continue
        try:
            number = int(entry.get("number"))
        except (TypeError, ValueError):
            continue
        source = str(entry.get("source", "")).strip()
        if number in seen or not source:
            continue
        if not 1 <= number <= requested_count:
            continue
        seen.add(number)
        blocks.append((number, source))

    blocks.sort(key=lambda item: item[0])
    if [n for n, _ in blocks] != list(range(1, requested_count + 1)):
        return []
    return [source for _, source in blocks]


def _normalize_outline_entry(text: str, chapter_num: int) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    fenced = re.match(r"^\s*```(?:[a-zA-Z]*)\n(.*)\n```\s*$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    heading = re.search(
        rf"(?im)^\s*##\s*Chapter\s+{chapter_num}\b[^\n]*$",
        text,
    )
    if heading:
        text = text[heading.start():].strip()
    return text


def _outline_entry_complete(text: str, chapter_num: int) -> bool:
    normalized = _normalize_outline_entry(text, chapter_num)
    if not normalized:
        return False
    if not re.search(rf"(?im)^\s*##\s*Chapter\s+{chapter_num}\b", normalized):
        return False
    plan_match = re.search(r"(?mi)^\s*Chapter Plan:\s*$", normalized)
    continuity_match = re.search(r"(?mi)^\s*Continuity:\s*$", normalized)
    if not plan_match or not continuity_match or continuity_match.start() <= plan_match.end():
        return False
    tail = normalized[continuity_match.end():].strip()
    if len(tail) < 20:
        return False
    return tail[-1] in ".!?\"')]»”`"


def _previous_continuity(entry: str) -> str:
    match = re.search(r"(?mi)^\s*Continuity:\s*$", entry or "")
    if not match:
        return "(none — this is the first chapter)"
    value = entry[match.end():].strip()
    return value or "(none)"


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


def _split_story(worker, requested_count: int) -> list[str]:
    story_source = _story_source(worker, requested_count)
    if len(story_source.strip()) < _MIN_SOURCE_CHARS:
        logger.warning("[generate_outline] story source is too short for semantic chapter splitting.")
        return []

    system = prompts.render(
        "outline/split_system",
        requested_count=requested_count,
    )
    user = prompts.render(
        "outline/split_user",
        requested_count=requested_count,
        story_source=story_source,
    )

    logger.info(
        "[generate_outline] Stage 1: splitting story draft into %d semantic chapter block(s).",
        requested_count,
    )
    raw = worker._run_lean_inference(
        TaskType.GENERATE_OUTLINE,
        system,
        user,
        max_tokens=_MAX_SPLIT_TOKENS,
    )
    blocks = _extract_chapter_blocks(raw, requested_count)
    if not blocks:
        logger.warning("[generate_outline] Stage 1 returned an invalid chapter partition.")
        return []

    logger.info(
        "[generate_outline] Stage 1 complete: %d semantic chapter block(s) produced.",
        len(blocks),
    )
    return blocks


def _build_chapter_context(worker, chapter_num: int, source_block: str, previous_entry: str) -> tuple[str, str]:
    language = worker._response_language()
    language_note = f" Write in {language}." if language else ""
    style = worker.project.writing_style.to_prompt_fragment().strip()
    characters = format_characters_block(worker.project.characters).strip() or "(none yet)"
    world = (worker.project.world or "").strip() or "(none yet)"
    previous = _previous_continuity(previous_entry) if previous_entry else "(none — this is the first chapter)"

    system = prompts.render(
        "outline/chapter_system",
        language_note=language_note,
    )
    user = prompts.render(
        "outline/chapter_user",
        chapter_number=chapter_num,
        source_block=source_block,
        author_style=style or "(none specified)",
        characters=characters,
        world=world,
        previous_continuity=previous,
    )
    return system, user


def _continue_chapter_outline(worker, chapter_num: int, source_block: str, partial: str, previous_entry: str) -> str:
    language = worker._response_language()
    language_note = f" Write in {language}." if language else ""
    style = worker.project.writing_style.to_prompt_fragment().strip()
    characters = format_characters_block(worker.project.characters).strip() or "(none yet)"
    world = (worker.project.world or "").strip() or "(none yet)"
    previous = _previous_continuity(previous_entry) if previous_entry else "(none — this is the first chapter)"

    system = prompts.render("outline/chapter_system", language_note=language_note)
    user = prompts.render(
        "outline/chapter_continue_user",
        chapter_number=chapter_num,
        source_block=source_block,
        author_style=style or "(none specified)",
        characters=characters,
        world=world,
        previous_continuity=previous,
        partial_outline=partial,
    )
    return worker._run_lean_inference(
        TaskType.GENERATE_OUTLINE,
        system,
        user,
        max_tokens=worker._content_max_tokens(),
    )


def _write_chapter_outline(worker, chapter_num: int, source_block: str, previous_entry: str) -> str:
    system, user = _build_chapter_context(worker, chapter_num, source_block, previous_entry)
    partial = worker._run_lean_inference(
        TaskType.GENERATE_OUTLINE,
        system,
        user,
        max_tokens=worker._content_max_tokens(),
    ).strip()
    if not partial:
        return ""
    partial = _normalize_outline_entry(partial, chapter_num)

    for pass_number in range(1, _MAX_OUTLINE_PASSES + 1):
        if _outline_entry_complete(partial, chapter_num):
            logger.info(
                "[generate_outline] Chapter %d outline complete after %d pass(es).",
                chapter_num,
                pass_number,
            )
            return partial

        logger.info(
            "[generate_outline] Chapter %d outline continues after pass %d.",
            chapter_num,
            pass_number,
        )
        addition = _continue_chapter_outline(
            worker,
            chapter_num,
            source_block,
            partial,
            previous_entry,
        ).strip()
        if not addition:
            break

        repeated_prefix = partial[-1200:].strip()
        if repeated_prefix and addition.startswith(repeated_prefix):
            addition = addition[len(repeated_prefix):].lstrip()
        if not addition:
            break
        partial = (partial.rstrip() + "\n\n" + addition).strip()
        partial = _normalize_outline_entry(partial, chapter_num)

    if _outline_entry_complete(partial, chapter_num):
        return partial
    logger.warning(
        "[generate_outline] Chapter %d outline remained incomplete after %d pass(es).",
        chapter_num,
        _MAX_OUTLINE_PASSES,
    )
    return ""


def run_generate_outline(worker) -> None:
    requested_count = worker._extract_requested_chapter_count(worker.extra_input)
    if requested_count is None or requested_count < 1:
        worker.error_occurred.emit(
            "Generate Outline requires a valid requested chapter count."
        )
        return

    worker.step_started.emit(
        f"Dividing story draft into {requested_count} chapter source blocks..."
    )
    source_blocks = _split_story(worker, requested_count)
    if len(source_blocks) != requested_count:
        worker.error_occurred.emit(
            "Could not reliably divide the story draft into the requested number "
            "of chapters. Nothing was saved — try again."
        )
        return

    generated_entries: list[str] = []
    previous_entry = ""

    for idx, source_block in enumerate(source_blocks, start=1):
        if worker._cancelled:
            return
        worker.step_started.emit(
            f"Writing outline for Chapter {idx}/{requested_count}..."
        )
        logger.info(
            "[generate_outline] Stage 2: Chapter %d/%d outline writer.",
            idx,
            requested_count,
        )
        entry = _write_chapter_outline(worker, idx, source_block, previous_entry)
        if not entry:
            worker.error_occurred.emit(
                f"Could not complete the outline for Chapter {idx}. Nothing was saved — try again."
            )
            return
        generated_entries.append(entry)
        previous_entry = entry

    outline_text = "\n\n".join(generated_entries).strip()
    worker.project.outline = outline_text

    # Preserve the existing post-processing stage: characters and world are
    # extracted from the finished outline after all chapter entries exist.
    worker._extract_and_merge_characters(outline_text)
    worker._update_world_incremental(outline_text, source_type="outline")
    storage.save_project(worker.project)
    worker.step_finished.emit("Outline", outline_text)
    logger.info(
        "[generate_outline] Generate Outline complete: %d chapter(s) written via two-stage pipeline.",
        requested_count,
    )


def install(module) -> None:
    if getattr(module, "_outline_generation_installed", False):
        return
    module._run_generate_outline = run_generate_outline
    module._outline_generation_installed = True
    logger.info("[generate_outline] Two-stage outline generation installed.")
