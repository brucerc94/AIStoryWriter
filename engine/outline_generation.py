"""Generate Outline and Extend Outline pipelines.

Both flows use the same chapter-treatment architecture:
source material -> semantic chapter chunks -> relevant canon selection ->
full Author Profile -> rich narrative treatment -> bounded continuation.

Generate Outline does not create or update Characters or World.
Extend Outline updates Characters and World only after the full extension
has been generated, using the user's original extension request as source.
"""

from __future__ import annotations

import json
import logging
import re

from engine.models import TaskType
from engine.context import build_relevant_chapter_context
from engine import prompts, storage

logger = logging.getLogger("workflow")

OUTLINE_SUGGESTION_MARKER = "__AI_STORY_WRITER_OUTLINE_SUGGESTION__"
OUTLINE_EXTEND_MARKER = "__AI_STORY_WRITER_OUTLINE_EXTEND__"

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


def _recover_malformed_chapter_json(raw: str, requested_count: int) -> dict | None:
    """Recover chapter objects when a local model emits invalid JSON prose.

    Local models sometimes put unescaped quotes inside the source strings or
    omit commas between adjacent chapter objects. Since the splitter's
    contract is structurally simple, recover each number/source pair directly
    from the raw text instead of asking the model to regenerate it.
    """
    text = (raw or "").strip()
    pattern = re.compile(
        r'(?m)(?:^|[\r\n])\s*\{\s*"number"\s*:\s*(\d+)\s*,\s*"source"\s*:\s*"',
        re.DOTALL,
    )
    matches = list(pattern.finditer(text))
    if len(matches) != requested_count:
        return None

    chapters: list[dict[str, object]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[start:end].rstrip()
        if not segment:
            return None

        if index + 1 < len(matches):
            # Normal or malformed inter-object ending: ..."}, or ..."}
            segment = re.sub(r'"\s*\}\s*,?\s*$', '', segment)
        else:
            # Final object normally ends with ..."}]}; tolerate whitespace.
            segment = re.sub(r'"\s*\]\s*\}\s*$', '', segment)
            if segment == text[start:end].rstrip():
                segment = re.sub(r'"\s*\}\s*\]\s*\}\s*$', '', segment)

        source = segment.strip()
        if not source:
            return None
        chapters.append({"number": int(match.group(1)), "source": source})

    return {"chapters": chapters}


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
        if isinstance(value, dict):
            return value, "ok"
        return None, f"top-level JSON type is {type(value).__name__!r}, expected object"
    except json.JSONDecodeError as first_error:
        first_message = str(first_error)
    cleaned = _clean_json_for_local_models(candidate)
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            logger.info("[generate_outline] Stage 1 JSON parsed after local-model cleanup.")
            return value, "ok"
        return None, f"top-level JSON type after cleanup is {type(value).__name__!r}"
    except json.JSONDecodeError as second_error:
        recovered = _recover_malformed_chapter_json(raw, requested_count=_infer_requested_count_from_raw(raw))
        if recovered is not None:
            logger.warning("[generate_outline] Stage 1 recovered malformed chapter JSON directly from raw output.")
            return recovered, "recovered malformed chapter JSON"
        return None, f"JSONDecodeError (original: {first_message!r}; after cleanup: {str(second_error)!r})"


def _infer_requested_count_from_raw(raw: str) -> int:
    text = raw or ""
    matches = re.findall(r'(?m)(?:^|[\r\n])\s*\{\s*"number"\s*:\s*(\d+)\s*,\s*"source"', text)
    return len(matches)


def _extract_chapter_blocks(raw: str, requested_count: int) -> list[str]:
    data, reason = _parse_json_object_with_count(raw, requested_count)
    if data is None:
        logger.error("[generate_outline] Stage 1 JSON parse failed — %s. Raw: %r", reason, (raw or "")[:1000])
        return []
    chapters = data.get("chapters")
    if not isinstance(chapters, list):
        logger.error("[generate_outline] Stage 1 JSON has no 'chapters' list. Keys=%s", list(data.keys()))
        return []
    blocks: list[tuple[int, str]] = []
    seen: set[int] = set()
    skipped: list[str] = []
    for index, entry in enumerate(chapters):
        if not isinstance(entry, dict):
            skipped.append(f"item[{index}] not a dict")
            continue
        try:
            number = int(entry.get("number"))
        except (TypeError, ValueError):
            skipped.append(f"item[{index}] invalid number")
            continue
        if number < 1 or number > requested_count:
            skipped.append(f"item[{index}] number {number} outside 1..{requested_count}")
            continue
        if number in seen:
            skipped.append(f"item[{index}] duplicate chapter {number}")
            continue
        source = str(entry.get("source", "")).strip()
        if not source:
            skipped.append(f"item[{index}] chapter {number} has empty source")
            continue
        seen.add(number)
        blocks.append((number, source))
    if skipped:
        logger.warning("[generate_outline] Stage 1 skipped entries: %s", "; ".join(skipped))
    blocks.sort(key=lambda item: item[0])
    numbers = [number for number, _ in blocks]
    expected = list(range(1, requested_count + 1))
    if numbers != expected:
        logger.error("[generate_outline] Stage 1 numbering mismatch. Expected=%s got=%s missing=%s", expected, numbers, sorted(set(expected) - set(numbers)))
        return []
    return [source for _, source in blocks]


def _parse_json_object_with_count(raw: str, requested_count: int) -> tuple[dict | None, str]:
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
        if isinstance(value, dict):
            return value, "ok"
        return None, f"top-level JSON type is {type(value).__name__!r}, expected object"
    except json.JSONDecodeError as first_error:
        first_message = str(first_error)
    cleaned = _clean_json_for_local_models(candidate)
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            logger.info("[generate_outline] Stage 1 JSON parsed after local-model cleanup.")
            return value, "ok"
        return None, f"top-level JSON type after cleanup is {type(value).__name__!r}"
    except json.JSONDecodeError as second_error:
        recovered = _recover_malformed_chapter_json(raw, requested_count)
        if recovered is not None:
            logger.warning("[generate_outline] Stage 1 recovered %d chapter object(s) from malformed raw JSON.", requested_count)
            return recovered, "recovered malformed chapter JSON"
        return None, f"JSONDecodeError (original: {first_message!r}; after cleanup: {str(second_error)!r})"


def _truncate_after_target_chapter(text: str, chapter_num: int) -> str:
    pattern = re.compile(r"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+(\d+)\b[^\n]*$")
    matches = list(pattern.finditer(text))
    if not matches:
        return text.strip()
    target_index = next((i for i, m in enumerate(matches) if int(m.group(1)) == chapter_num), None)
    if target_index is None:
        return text.strip()
    start = matches[target_index].start()
    end = matches[target_index + 1].start() if target_index + 1 < len(matches) else len(text)
    return text[start:end].strip()


def _normalize_outline_entry(text: str, chapter_num: int) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    fenced = re.match(r"^\s*```(?:[a-zA-Z]*)\n(.*)\n```\s*$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    heading = re.search(rf"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+{chapter_num}\b[^\n]*$", text)
    if heading:
        text = text[heading.start():].strip()
    return _truncate_after_target_chapter(text, chapter_num)


def _strip_duplicate_heading(text: str, chapter_num: int) -> str:
    return re.sub(rf"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+{chapter_num}\b[^\n]*\s*", "", (text or "").strip(), count=1).strip()


def _treatment_complete(text: str, chapter_num: int) -> bool:
    normalized = _normalize_outline_entry(text, chapter_num)
    if not normalized:
        return False
    body = _strip_duplicate_heading(normalized, chapter_num)
    if len(body) < _MIN_TREATMENT_CHARS:
        return False
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    if len(paragraphs) < 2:
        return False
    return bool(re.search(r'[.!?"\')\]]\s*$', body))


def _previous_chapter_context(entry: str) -> str:
    text = (entry or "").strip()
    return text[-_CONTINUATION_CHECKPOINT_CHARS:].lstrip() if text else "(none — this is the first chapter)"


def _continuation_checkpoint(partial: str) -> str:
    text = (partial or "").strip()
    return text[-_CONTINUATION_CHECKPOINT_CHARS:].lstrip()


def _sanitize_continuation_addition(addition: str, chapter_num: int) -> str:
    text = (addition or "").strip()
    if not text:
        return ""
    fenced = re.match(r"^\s*```(?:[a-zA-Z]*)\n(.*)\n```\s*$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    text = re.sub(rf"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+{chapter_num}\b[^\n]*\s*", "", text, count=1)
    next_heading = re.search(rf"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+{chapter_num + 1}\b[^\n]*$", text)
    if next_heading:
        text = text[:next_heading.start()].rstrip()
    text = re.sub(r"(?im)^\s*(?:Chapter Plan|Narrative Treatment|Continuity)\s*:\s*$", "", text)
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
    characters, world = build_relevant_chapter_context(worker.project, (source_block or "").strip(), max_character_chars=_MAX_CHARACTER_CHARS, max_world_chars=_MAX_WORLD_CHARS)
    logger.info("[generate_outline] Relevant canon from current chunk: characters=%d chars, world=%d chars.", len(characters), len(world))
    return characters or "(none relevant)", world or "(none relevant)"


def _build_chapter_prompt(worker, chapter_num: int, source_block: str, previous_entry: str, characters: str, world: str) -> tuple[str, str]:
    language = worker._response_language()
    language_note = f" Write in {language}." if language else ""
    return (
        prompts.render("outline/chapter_system", language_note=language_note),
        prompts.render("outline/chapter_user", chapter_number=chapter_num, source_block=source_block, author_profile=_build_author_profile(worker), characters=characters, world=world, previous_continuity=_previous_chapter_context(previous_entry)),
    )


def _build_continuation_prompt(worker, chapter_num: int, source_block: str, partial: str, previous_entry: str, characters: str, world: str) -> tuple[str, str]:
    language = worker._response_language()
    language_note = f" Write in {language}." if language else ""
    return (
        prompts.render("outline/chapter_system", language_note=language_note),
        prompts.render("outline/chapter_continue_user", chapter_number=chapter_num, source_block=source_block, author_profile=_build_author_profile(worker), characters=characters, world=world, previous_continuity=_previous_chapter_context(previous_entry), partial_checkpoint=_continuation_checkpoint(partial)),
    )


def _write_chapter_outline(worker, chapter_num: int, source_block: str, previous_entry: str) -> str:
    characters, world = _select_relevant_canon(worker, source_block)
    system, user = _build_chapter_prompt(worker, chapter_num, source_block, previous_entry, characters, world)
    partial = worker._run_lean_inference(TaskType.GENERATE_OUTLINE, system, user, max_tokens=worker._content_max_tokens()).strip()
    if not partial:
        logger.error("[generate_outline] Chapter %d returned empty output.", chapter_num)
        return ""
    partial = _normalize_outline_entry(partial, chapter_num)
    for continuation_pass in range(1, _MAX_OUTLINE_PASSES + 1):
        if _treatment_complete(partial, chapter_num):
            logger.info("[generate_outline] Chapter %d treatment complete after %d inference(s).", chapter_num, continuation_pass)
            return partial
        system, user = _build_continuation_prompt(worker, chapter_num, source_block, partial, previous_entry, characters, world)
        addition = worker._run_lean_inference(TaskType.GENERATE_OUTLINE, system, user, max_tokens=worker._content_max_tokens()).strip()
        addition = _sanitize_continuation_addition(addition, chapter_num)
        if not addition:
            break
        partial = _normalize_outline_entry((partial.rstrip() + "\n\n" + addition).strip(), chapter_num)
    return partial if _treatment_complete(partial, chapter_num) else ""


def _story_source(worker, requested_count: int, source_text: str | None = None) -> str:
    source = (source_text if source_text is not None else (worker.project.synopsis or "")).strip()
    if source_text is None:
        request = (worker.extra_input or "").strip()
        parts = [p for p in (f"STORY DRAFT / SYNOPSIS:\n{source}" if source else "", f"AUTHOR STORY INPUT:\n{request}" if request else "", f"REQUESTED CHAPTER COUNT: {requested_count}") if p]
        return "\n\n".join(parts).strip()
    return f"STORY MATERIAL TO EXTEND:\n{source}\n\nREQUESTED NEW CHAPTER COUNT: {requested_count}".strip()


def _split_story(worker, requested_count: int, story_source: str) -> list[str]:
    if len(story_source.strip()) < _MIN_SOURCE_CHARS:
        logger.warning("[generate_outline] source is too short for semantic chapter splitting.")
        return []
    system = prompts.render("outline/split_system", requested_count=requested_count)
    user = prompts.render("outline/split_user", requested_count=requested_count, story_source=story_source)
    logger.info("[generate_outline] Stage 1: semantic split into %d chapter source block(s).", requested_count)
    raw = worker._run_lean_inference(TaskType.GENERATE_OUTLINE, system, user, max_tokens=_MAX_SPLIT_TOKENS)
    logger.info("[generate_outline] Stage 1 raw output: %d tokens-equivalent, %d chars. Preview=%r", len(raw.split()), len(raw), raw[:300])
    blocks = _extract_chapter_blocks(raw, requested_count)
    if not blocks:
        logger.error("[generate_outline] Stage 1 failed to produce a valid partition for %d chapter(s).", requested_count)
        return []
    logger.info("[generate_outline] Stage 1 complete: %d block(s).", len(blocks))
    return blocks


def _extract_requested_chapter_count(worker) -> int | None:
    match = re.search(r"EXACTLY\s+(\d+)\s+chapters", worker.extra_input or "", re.IGNORECASE)
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
    source_blocks = _split_story(worker, requested_count, _story_source(worker, requested_count, synopsis))
    if len(source_blocks) != requested_count:
        worker.error_occurred.emit("Could not reliably divide the story draft into the requested number of chapters. Nothing was saved — try again.")
        return
    generated_entries: list[str] = []
    previous_entry = ""
    for chapter_num, source_block in enumerate(source_blocks, start=1):
        if worker._cancelled:
            return
        worker.step_started.emit(f"Writing outline for Chapter {chapter_num}/{requested_count}...")
        entry = _write_chapter_outline(worker, chapter_num, source_block, previous_entry)
        if not entry:
            worker.error_occurred.emit(f"Could not complete the outline for Chapter {chapter_num}. Nothing was saved — try again.")
            return
        generated_entries.append(entry)
        previous_entry = entry
    worker.project.outline = "\n\n".join(generated_entries).strip()
    storage.save_project(worker.project)
    worker.step_finished.emit("Outline", worker.project.outline)
    logger.info("[generate_outline] Generate Outline complete: %d chapter(s). No Character/World creation or update performed.", requested_count)


def _parse_extend_payload(worker) -> tuple[int, str] | None:
    payload = worker.extra_input.split(OUTLINE_EXTEND_MARKER, 1)[1].strip("\n")
    count_line, _, request = payload.partition("\n")
    try:
        count = int(count_line.strip())
    except ValueError:
        return None
    if count < 1 or not request.strip():
        return None
    return count, request.strip()


def _last_outline_entry(outline: str) -> str:
    text = (outline or "").strip()
    matches = list(re.finditer(r"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+(\d+)\b[^\n]*$", text))
    return text[matches[-1].start():].strip() if matches else text[-_CONTINUATION_CHECKPOINT_CHARS:].strip()


def _run_extend_outline(worker) -> None:
    parsed = _parse_extend_payload(worker)
    if parsed is None:
        worker.error_occurred.emit("Extend Outline requires a valid chapter count and story material.")
        return
    requested_count, user_request = parsed
    current_outline = (worker.project.outline or "").strip()
    if not current_outline:
        worker.error_occurred.emit("There is no outline to extend. Generate an outline first.")
        return
    chapter_numbers = [int(match.group(1)) for match in re.finditer(r"(?im)^\s*##\s*(?:Chapter|Cap[ií]tulo)\s+(\d+)\b", current_outline)]
    next_chapter = max(chapter_numbers, default=0) + 1
    split_source = _story_source(worker, requested_count, user_request)
    worker.step_started.emit(f"Dividing extension request into {requested_count} chapter source blocks...")
    source_blocks = _split_story(worker, requested_count, split_source)
    if len(source_blocks) != requested_count:
        worker.error_occurred.emit("Could not reliably divide the extension request into the requested number of chapters. Nothing was appended — try again.")
        return
    generated_entries: list[str] = []
    previous_entry = _last_outline_entry(current_outline)
    for index, source_block in enumerate(source_blocks):
        if worker._cancelled:
            return
        chapter_num = next_chapter + index
        worker.step_started.emit(f"Writing outline for Chapter {chapter_num}/{next_chapter + requested_count - 1}...")
        entry = _write_chapter_outline(worker, chapter_num, source_block, previous_entry)
        if not entry:
            worker.error_occurred.emit(f"Could not complete the outline for Chapter {chapter_num}. Nothing was appended — try again.")
            return
        generated_entries.append(entry)
        previous_entry = entry
    new_outline = "\n\n".join(generated_entries).strip()
    if len(generated_entries) != requested_count:
        worker.error_occurred.emit(f"Expected {requested_count} new chapters but generated {len(generated_entries)}.")
        return
    worker.project.outline = (current_outline.rstrip() + "\n\n" + new_outline).strip()

    logger.info("[extend_outline] Updating Characters and World from the user's original extension request.")
    worker._extract_and_merge_characters(user_request)
    worker._update_world_incremental(user_request, source_type="outline_extend_input")

    storage.save_project(worker.project)
    worker.step_finished.emit(f"Outline Extended (Chapters {next_chapter}-{next_chapter + requested_count - 1})", new_outline)
    logger.info("[extend_outline] Extend Outline complete: %d chapter(s); canon updated from user input.", requested_count)


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
    logger.info("[generate_outline] Shared rich Generate/Extend Outline pipeline installed.")
