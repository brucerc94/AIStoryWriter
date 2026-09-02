"""
CHANGE_CHAPTER workflow: continuity summary → checklist plan → full rewrite
with clean context → checklist evaluation → continuation until complete.

Characters/World/Memory are refreshed only once the chapter is accepted.
All prompt text lives in engine/prompts/change_chapter/.

Shared helpers (parse_checklist_items, ends_abruptly, trim_leading_overlap,
is_substantial_duplicate) are also used by WRITE_CHAPTER in workflow.py,
which follows the same planner → write → evaluate → continue pattern.
"""

from __future__ import annotations

import json
import logging
import re

from engine import prompts
from engine.context import extract_outline_section, format_characters_block
from engine.models import ChatMessage, MessageRole, TaskType

logger = logging.getLogger("workflow")

MAX_CHANGE_EVAL_RETRIES = 3          # attempts before declaring an evaluation invalid
MAX_CHANGE_PLAN_TOKENS = 2048
MAX_CHANGE_PASSES = 10
# Flat eval budget; each checklist item needs ~80 chars of JSON.
MAX_CHANGE_EVAL_TOKENS = 3072
# Near-duplicate threshold: 60% of the tail must appear verbatim at the start of the continuation.
_DUPLICATE_OVERLAP_RATIO = 0.60
# Maximum consecutive invalid evaluations before stopping the loop.
MAX_CONSECUTIVE_INVALID_EVALS = 2

# Section size caps to prevent context overflow regardless of model settings.
PREV_CHAPTER_CAP = 14000
WORLD_CAP = 6000
MEMORY_CAP = 5000
CHAPTER_CAP = 20000


def _chapter_title(worker, chapter_num: int) -> str:
    return next(
        (c.title for c in worker.project.chapters if c.number == chapter_num),
        f"Chapter {chapter_num}",
    )


def _cap(text: str, limit: int, label: str) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return f"[... earlier {label} omitted for length ...]\n\n" + text[-limit:]


# ---------------------------------------------------------------------------
# STAGE 1 — CONTINUITY SUMMARY
# ---------------------------------------------------------------------------

NO_PREVIOUS_CHAPTER = "No previous chapter. This is the beginning of the story."


def summarize_previous_chapter(worker, chapter_num: int) -> str:
    """Summarise the previous chapter for continuity context. Not saved or shown in chat."""
    if chapter_num <= 1:
        return NO_PREVIOUS_CHAPTER

    prev = next((c for c in worker.project.chapters if c.number == chapter_num - 1), None)
    if not prev or not prev.content.strip():
        return NO_PREVIOUS_CHAPTER

    prev_content = _cap(prev.content, PREV_CHAPTER_CAP, "part of the chapter")
    language = worker._response_language()

    system = prompts.render(
        "change_chapter/summarize_previous_chapter_system",
        language_note=f" Write the summary in {language}." if language else "",
    )
    user = prompts.render(
        "change_chapter/summarize_previous_chapter_user",
        chapter_number=prev.number,
        chapter_title=prev.title.strip() or "Untitled",
        chapter_content=prev_content,
    )

    logger.info("[change_chapter] Stage 1: summarising Chapter %d for continuity.", prev.number)
    worker.step_started.emit(f"Summarising Chapter {prev.number} for continuity context...")

    summary = worker._run_lean_inference(TaskType.CHANGE_CHAPTER, system, user, max_tokens=1024)
    if not summary or not summary.strip():
        logger.warning("[change_chapter] Stage 1: summary call returned empty — using sentinel instead.")
        return NO_PREVIOUS_CHAPTER
    return summary.strip()


# ---------------------------------------------------------------------------
# STAGE 2 — PLANNER (checklist)
# ---------------------------------------------------------------------------

def plan_change(worker, chapter_num: int, chapter_content: str, instruction: str) -> str:
    system = prompts.render("change_chapter/plan_system")
    user = prompts.render(
        "change_chapter/plan_user",
        chapter_number=chapter_num,
        chapter_title=_chapter_title(worker, chapter_num),
        instruction=instruction.strip(),
        chapter_content=chapter_content,
    )
    raw = worker._run_lean_inference(TaskType.CHANGE_CHAPTER, system, user, max_tokens=MAX_CHANGE_PLAN_TOKENS)
    plan = (raw or "").strip()
    if not plan:
        logger.warning("[change_chapter] Planner returned no checklist; using the original request directly.")
        return ""
    logger.info("[change_chapter] Internal checklist for Chapter %d:\n%s", chapter_num, plan)
    worker.step_started.emit(f"Change Chapter checklist — Chapter {chapter_num}:\n{plan}")
    return plan


def parse_checklist_items(checklist: str) -> list[tuple[int, str]]:
    """Parse the planner's numbered checklist into stable (id, text) pairs."""
    items: list[tuple[int, str]] = []
    seen: set[int] = set()
    for match in re.finditer(r"^\s*(\d+)\s*[.)-]\s+(.+?)\s*$", checklist or "", re.MULTILINE):
        item_id = int(match.group(1))
        text = match.group(2).strip()
        if item_id in seen or not text:
            continue
        seen.add(item_id)
        items.append((item_id, text))
    return items


# ---------------------------------------------------------------------------
# STAGE 3 — FULL REWRITE WITH CLEAN CONTEXT
# ---------------------------------------------------------------------------

def build_clean_context_sections(worker, chapter_num: int, prev_summary: str) -> str:
    """Assemble the clean context block (no full outline, chat history, or previous chapter text)."""
    project = worker.project

    characters = format_characters_block(project.characters[:12]) or "(none)"
    world = _cap(project.world, WORLD_CAP, "world notes") or "(none)"
    memory = _cap(project.memory, MEMORY_CAP, "story memory") or "(none)"
    intent_frag = project.author_intent.to_prompt_fragment()
    style_frag = project.writing_style.to_prompt_fragment()
    chapter_outline = extract_outline_section(project.outline, chapter_num) or "(no outline entry for this chapter)"

    sections = [
        prompts.render("change_chapter/section", heading="STORY CONTINUITY", body=f"Previous Chapter Summary:\n{prev_summary}"),
        prompts.render("change_chapter/section", heading="CHARACTERS", body=characters),
    ]
    if world != "(none)":
        sections.append(prompts.render("change_chapter/section", heading="WORLD & SETTING", body=world))
    if memory != "(none)":
        sections.append(prompts.render("change_chapter/section", heading="STORY MEMORY", body=memory))
    if intent_frag:
        sections.append(prompts.render("change_chapter/section", heading="AUTHOR INTENT", body=intent_frag))
    if style_frag:
        sections.append(prompts.render("change_chapter/section", heading="WRITING STYLE", body=style_frag))
    sections.append(prompts.render("change_chapter/section", heading="CURRENT CHAPTER PLAN", body=chapter_outline))

    return "\n\n".join(sections)


def build_full_rewrite_prompt(
    worker, chapter_num: int, chapter_content: str, instruction: str, checklist: str, prev_summary: str,
) -> tuple[str, str]:
    style_frag = worker.project.writing_style.to_prompt_fragment()
    language = worker._response_language()

    system = prompts.render(
        "change_chapter/full_rewrite_system",
        language_note=f" Write the chapter in {language}." if language else "",
        style_block=f"\n\nWriting style to preserve:\n{style_frag}" if style_frag else "",
    )
    user = prompts.render(
        "change_chapter/full_rewrite_user",
        chapter_number=chapter_num,
        chapter_title=_chapter_title(worker, chapter_num),
        instruction=instruction.strip(),
        checklist=checklist.strip() or "(none; follow the original request directly)",
        context_sections=build_clean_context_sections(worker, chapter_num, prev_summary),
        chapter_content=_cap(chapter_content, CHAPTER_CAP, "part of the chapter"),
    )
    return system, user


# ---------------------------------------------------------------------------
# STAGE 4 — EVALUATOR
# ---------------------------------------------------------------------------

def _build_eval_prompt(worker, chapter_num: int, chapter_content: str, instruction: str, checklist: str) -> tuple[str, str]:
    items = parse_checklist_items(checklist)
    item_block = "\n".join(f"{item_id}. {text}" for item_id, text in items) or \
        "(No parseable checklist items; evaluate the original request directly.)"
    all_ids = [item_id for item_id, _ in items]
    id_list = ", ".join(str(i) for i in all_ids) if all_ids else "(none)"

    system = prompts.render("change_chapter/eval_system", id_list=id_list)
    user = prompts.render(
        "change_chapter/eval_user",
        chapter_number=chapter_num,
        chapter_title=_chapter_title(worker, chapter_num),
        instruction=instruction.strip(),
        checklist_items=item_block,
        chapter_content=chapter_content,
        id_list=id_list,
    )
    return system, user


def parse_checklist_eval_result(text: str, checklist: str) -> dict:
    """
    Parse the evaluator's JSON response into:
      valid       – whether a structured result was obtained
      completed   – True only when ALL checklist IDs are in done and missing is empty
      missing     – human-readable missing requirement strings
      missing_ids – int IDs still incomplete
      done_ids    – int IDs confirmed complete
      raw         – raw evaluator text (for debugging)
    """
    raw = (text or "").strip()
    expected_items = dict(parse_checklist_items(checklist))

    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(raw[start:end + 1])

            done_ids: set[int] = set()
            for value in data.get("done", []) if isinstance(data.get("done", []), list) else []:
                try:
                    done_ids.add(int(value))
                except (TypeError, ValueError):
                    continue

            missing: list[str] = []
            missing_ids: set[int] = set()
            for entry in data.get("missing", []) if isinstance(data.get("missing", []), list) else []:
                if isinstance(entry, dict):
                    try:
                        item_id = int(entry.get("id"))
                    except (TypeError, ValueError):
                        continue
                    missing_ids.add(item_id)
                    why = str(entry.get("why", "") or "").strip()
                    label = expected_items.get(item_id, f"Checklist item {item_id}")
                    missing.append(f"{item_id}: {why or label}")
                else:
                    s = str(entry).strip()
                    if s:
                        missing.append(s)

            # Every expected checklist ID must be accounted for.
            unaccounted = set(expected_items) - done_ids - missing_ids
            for item_id in sorted(unaccounted):
                missing_ids.add(item_id)
                missing.append(f"{item_id}: not evaluated — {expected_items[item_id]}")

            # An ID both done AND missing is a contradiction — treat as missing.
            contradictory = done_ids & missing_ids
            if contradictory:
                for item_id in sorted(contradictory):
                    done_ids.discard(item_id)
                missing.append("Evaluator contradiction on IDs: " + ", ".join(map(str, sorted(contradictory))))

            if 0 in missing_ids and "Chapter appears truncated." not in missing:
                missing.append("Chapter appears truncated.")

            all_items_complete = bool(expected_items) and set(expected_items).issubset(done_ids) and not missing_ids
            model_said_complete = bool(data.get("complete", False))
            if model_said_complete and not all_items_complete:
                logger.warning("[change_chapter] Model returned complete=true but ID accounting disagrees — overriding to incomplete.")

            completed = all_items_complete if expected_items else (model_said_complete and not missing_ids)

            return {
                "valid": True, "completed": completed, "missing": missing,
                "missing_ids": sorted(missing_ids), "done_ids": sorted(done_ids), "raw": raw,
            }
    except Exception as exc:
        logger.debug("[change_chapter] Could not parse evaluator JSON: %s", exc)

    return {"valid": False, "completed": False, "missing": [], "missing_ids": [], "done_ids": [], "raw": raw}


def evaluate_change(worker, chapter_num: int, chapter_content: str, instruction: str, checklist: str, pass_number: int) -> dict:
    """Run the evaluator with retries. Returns valid=False if every attempt fails to parse."""
    system, user = _build_eval_prompt(worker, chapter_num, chapter_content, instruction, checklist)
    logger.info("[change_chapter] Chapter %d — verification pass %d — %d words.", chapter_num, pass_number, len(chapter_content.split()))

    item_count = max(1, len(parse_checklist_items(checklist)))
    max_eval_tokens = min(MAX_CHANGE_EVAL_TOKENS, max(768, 512 + item_count * 100))

    final = {"valid": False, "completed": False, "missing": [], "missing_ids": [], "done_ids": [], "raw": ""}
    for attempt in range(1, MAX_CHANGE_EVAL_RETRIES + 1):
        raw = worker._run_lean_inference(TaskType.REVIEW_CHAPTER, system, user, max_tokens=max_eval_tokens)
        parsed = parse_checklist_eval_result(raw, checklist)
        if parsed["valid"]:
            final = parsed
            logger.info(
                "[change_chapter] Attempt %d/%d: complete=%s done=%s missing_ids=%s",
                attempt, MAX_CHANGE_EVAL_RETRIES, parsed["completed"], parsed.get("done_ids", []), parsed.get("missing_ids", []),
            )
            break
        logger.warning(
            "[change_chapter] Attempt %d/%d returned invalid/unparseable result: %r",
            attempt, MAX_CHANGE_EVAL_RETRIES, (raw or "").strip()[:200],
        )

    if final["valid"]:
        if final["completed"]:
            worker.step_started.emit(f"Change Chapter verification — COMPLETE (pass {pass_number})")
        else:
            missing_text = "\n".join(f"- {item}" for item in final["missing"])
            worker.step_started.emit(
                f"Change Chapter verification — INCOMPLETE (pass {pass_number})\n"
                f"Missing:\n{missing_text or '- evaluator did not identify specific items'}"
            )
    else:
        worker.step_started.emit(
            f"Change Chapter verification — PARSE FAILURE (pass {pass_number}); "
            f"evaluator did not return valid JSON after {MAX_CHANGE_EVAL_RETRIES} attempts."
        )
    return final


# ---------------------------------------------------------------------------
# STAGE 5 — CONTINUATION (never SEARCH/REPLACE — always appends prose)
# ---------------------------------------------------------------------------

def ends_abruptly(text: str) -> bool:
    stripped = (text or "").rstrip()
    if not stripped:
        return True
    if len(stripped) <= 80:
        return False
    return not bool(re.search(r'[.!?\u2026"\')\]}\u201d\u2019]$', stripped))


def _build_continuation_prompt(worker, chapter_num: int, chapter_content: str, instruction: str, checklist: str, missing: list[str] | None) -> tuple[str, str]:
    tail = chapter_content[-7000:].strip()
    missing_text = "\n".join(f"- {item}" for item in (missing or [])) or "(none identified; verify the original request yourself)"
    language = worker._response_language()

    system = prompts.render("change_chapter/continue_system", language_note=f" Continue in {language}." if language else "")
    user = prompts.render(
        "change_chapter/continue_user",
        chapter_number=chapter_num,
        chapter_title=_chapter_title(worker, chapter_num),
        instruction=instruction.strip(),
        checklist=checklist.strip() or "(none)",
        missing_text=missing_text,
        chapter_tail=tail,
    )
    return system, user


def trim_leading_overlap(addition: str, tail: str, ratio: float = _DUPLICATE_OVERLAP_RATIO) -> str:
    """Strip a leading echo of `tail` from `addition`, if present."""
    if not addition or not tail:
        return addition
    max_overlap = int(len(tail) * ratio)
    if max_overlap < 40:
        return addition
    norm_add = re.sub(r"\s+", " ", addition).strip()
    norm_tail = re.sub(r"\s+", " ", tail).strip()
    suffix_len = min(max_overlap, len(norm_tail))
    while suffix_len >= 40:
        candidate_suffix = norm_tail[-suffix_len:]
        if norm_add.startswith(candidate_suffix):
            trim_len = len(candidate_suffix)
            trimmed = addition[trim_len:].lstrip()
            logger.info("[change_chapter] Trimmed %d chars of leading repeated text from continuation.", trim_len)
            return trimmed
        suffix_len -= 20
    return addition


def is_substantial_duplicate(addition: str, tail: str) -> bool:
    norm_add = re.sub(r"\s+", " ", addition.lower()).strip()
    norm_tail = re.sub(r"\s+", " ", tail.lower()).strip()
    if not norm_add:
        return True
    return norm_add == norm_tail or norm_add in norm_tail or norm_tail.endswith(norm_add)


def continue_rewrite(worker, chapter_num: int, chapter, instruction: str, checklist: str, pass_number: int, missing: list[str] | None = None) -> bool:
    """Append a continuation to chapter.content. Returns True if new content was appended."""
    worker.step_started.emit(f"Continuing Chapter {chapter_num} rewrite (pass {pass_number})...")
    system, user = _build_continuation_prompt(worker, chapter_num, chapter.content, instruction, checklist, missing)
    result = worker._run_lean_inference(TaskType.CHANGE_CHAPTER, system, user, max_tokens=worker._content_max_tokens())
    if not result or not result.strip():
        logger.warning("[change_chapter] Continuation returned empty text.")
        return False

    addition = result.strip()
    tail = chapter.content[-2500:].strip()
    addition = trim_leading_overlap(addition, tail)
    if is_substantial_duplicate(addition, tail):
        logger.warning("[change_chapter] Continuation is a near-duplicate of existing prose; refusing to append.")
        return False
    if not addition.strip():
        logger.warning("[change_chapter] Continuation was empty after overlap trim.")
        return False

    chapter.content = (chapter.content.rstrip() + "\n\n" + addition).strip()
    chapter.reviewed = False
    logger.info("[change_chapter] Added %d words during continuation pass %d.", len(addition.split()), pass_number)
    return True


# ---------------------------------------------------------------------------
# ORCHESTRATOR
# ---------------------------------------------------------------------------

def run(worker) -> None:
    """
    Full CHANGE_CHAPTER workflow, called directly from
    WorkflowWorker._run_change_chapter (no monkeypatching).
    """
    chapter_num = worker.project.current_chapter or len(worker.project.chapters)
    chapter = next((c for c in worker.project.chapters if c.number == chapter_num), None)
    if not chapter:
        worker.error_occurred.emit(f"Chapter {chapter_num} not found.")
        return
    if not chapter.content.strip():
        worker.error_occurred.emit(f"Chapter {chapter_num} has no content yet. Write it first.")
        return
    if not worker.extra_input.strip():
        worker.error_occurred.emit("No change instructions provided.")
        return

    instruction = worker.extra_input.strip()
    original_content = chapter.content

    # Stage 1: continuity summary of the previous chapter.
    prev_summary = summarize_previous_chapter(worker, chapter_num)

    # Stage 2: plan — checklist BEFORE rewriting anything.
    worker.step_started.emit(f"Planning Change Chapter {chapter_num}...")
    checklist = plan_change(worker, chapter_num, original_content, instruction)

    # Stage 3: full rewrite with clean context.
    worker.step_started.emit(f"Rewriting Chapter {chapter_num} with your changes...")
    system, user = build_full_rewrite_prompt(worker, chapter_num, original_content, instruction, checklist, prev_summary)
    rewritten = worker._run_lean_inference(TaskType.CHANGE_CHAPTER, system, user, max_tokens=worker._content_max_tokens())
    if not rewritten or not rewritten.strip():
        worker.error_occurred.emit("The model returned no rewritten chapter.")
        return

    chapter.content = rewritten.strip()
    chapter.reviewed = False

    worker.project.chat_messages.append(ChatMessage(role=MessageRole.USER, content=instruction))
    worker.project.chat_messages.append(ChatMessage(
        role=MessageRole.ASSISTANT,
        content=f"*(Rewrote the complete Chapter {chapter_num} according to your instructions.)*",
    ))

    # Stage 4/5: evaluate -> continue loop. Characters/World/Memory are
    # deliberately NOT refreshed until the chapter is accepted below.
    consecutive_invalid_evals = 0
    for pass_number in range(1, MAX_CHANGE_PASSES + 1):
        evaluation = evaluate_change(worker, chapter_num, chapter.content, instruction, checklist, pass_number)

        if not evaluation["valid"]:
            consecutive_invalid_evals += 1
            if consecutive_invalid_evals >= MAX_CONSECUTIVE_INVALID_EVALS:
                logger.error("[change_chapter] Stopping after %d consecutive invalid evaluations.", consecutive_invalid_evals)
                worker.step_started.emit(
                    "Change Chapter stopped: evaluator repeatedly failed to produce "
                    "valid JSON. The current chapter state has been saved."
                )
                break
            continue
        consecutive_invalid_evals = 0

        # A truncated chapter can never exit as "complete", even if the
        # evaluator says so — check this before trusting evaluation["completed"].
        if ends_abruptly(chapter.content):
            note = "Chapter is truncated mid-sentence — must be continued to a natural ending."
            if evaluation["completed"]:
                logger.warning("[change_chapter] Pass %d: evaluator said complete=true but chapter ends abruptly — overriding.", pass_number)
                evaluation["completed"] = False
            if not any("truncat" in m.lower() for m in evaluation["missing"]):
                evaluation["missing"].append(note)
                evaluation["missing_ids"] = sorted(set(evaluation.get("missing_ids", [])) | {0})
            worker.step_started.emit(f"Change Chapter verification — TRUNCATION DETECTED (pass {pass_number}); forcing continuation.")

        if evaluation["completed"]:
            worker._finalize_changed_chapter(chapter_num, chapter)
            from engine import storage
            storage.save_project(worker.project)
            worker.step_finished.emit(f"Rewrote and verified Chapter {chapter_num}", chapter.content)
            return

        if pass_number >= MAX_CHANGE_PASSES:
            logger.warning("[change_chapter] Reached MAX_CHANGE_PASSES=%d; saving current result.", MAX_CHANGE_PASSES)
            break

        continued = continue_rewrite(worker, chapter_num, chapter, instruction, checklist, pass_number + 1, evaluation.get("missing", []))
        if not continued:
            logger.warning("[change_chapter] Continuation produced no new content on pass %d; stopping.", pass_number + 1)
            break

    # Reached only on limit / early stop — finalize and save regardless.
    worker._finalize_changed_chapter(chapter_num, chapter)
    from engine import storage
    storage.save_project(worker.project)
    worker.step_finished.emit(f"Updated Chapter {chapter_num} (continuation limit reached)", chapter.content)


# ---------------------------------------------------------------------------
# WRITE_CHAPTER — same planner/evaluator philosophy as CHANGE_CHAPTER above,
# sourced from the chapter's outline entry instead of a user instruction.
# See the module docstring for how this relates to the rest of the file.
# The generation and continuation calls themselves stay in
# engine/workflow.py (_run_write_chapter), since they use the app's normal
# full-context pipeline rather than this module's "clean context" builder.
# ---------------------------------------------------------------------------

def plan_chapter(worker, chapter_num: int, chapter_title: str, outline_entry: str) -> str:
    """
    Derive an internal checklist of concrete requirements for a chapter's
    first draft from its outline entry (Story Progression / Continuity /
    Restrictions). Mirrors plan_change() above — same idea, different
    requirement source. Never shown to the user beyond the progress log,
    never saved as a permanent document.
    """
    system = prompts.render("write_chapter/plan_system")
    user = prompts.render(
        "write_chapter/plan_user",
        chapter_number=chapter_num,
        chapter_title=chapter_title,
        outline_entry=outline_entry.strip() or "(no outline entry available)",
    )
    raw = worker._run_lean_inference(TaskType.WRITE_CHAPTER, system, user, max_tokens=MAX_CHANGE_PLAN_TOKENS)
    checklist = (raw or "").strip()
    if checklist:
        logger.info("[write_chapter] Internal checklist for Chapter %d:\n%s", chapter_num, checklist)
        worker.step_started.emit(f"Write Chapter checklist — Chapter {chapter_num}:\n{checklist}")
    else:
        logger.warning("[write_chapter] Planner returned no checklist for Chapter %d; falling back to outline-only evaluation.", chapter_num)
    return checklist


def _build_chapter_eval_prompt(
    chapter_num: int, chapter_title: str, chapter_content: str, outline_entry: str, checklist: str,
) -> tuple[str, str]:
    items = parse_checklist_items(checklist)
    item_block = "\n".join(f"{item_id}. {text}" for item_id, text in items) or \
        "(No parseable checklist items; evaluate against the outline entry directly.)"
    all_ids = [item_id for item_id, _ in items]
    id_list = ", ".join(str(i) for i in all_ids) if all_ids else "(none)"

    system = prompts.render("write_chapter/eval_system", id_list=id_list)
    user = prompts.render(
        "write_chapter/eval_user",
        chapter_number=chapter_num,
        chapter_title=chapter_title,
        outline_entry=outline_entry.strip() or "(none)",
        checklist_items=item_block,
        chapter_content=chapter_content,
        id_list=id_list,
    )
    return system, user


def evaluate_chapter(
    worker, chapter_num: int, chapter_title: str, chapter_content: str,
    outline_entry: str, checklist: str, pass_number: int,
) -> dict:
    """Run the WRITE_CHAPTER checklist evaluator with retries. Same JSON
    contract and parsing as evaluate_change() above (see
    parse_checklist_eval_result), so a valid=False result here means every
    attempt failed to produce parseable JSON."""
    system, user = _build_chapter_eval_prompt(chapter_num, chapter_title, chapter_content, outline_entry, checklist)
    logger.info("[write_chapter] Chapter %d — checklist verification pass %d — %d words.", chapter_num, pass_number, len(chapter_content.split()))

    item_count = max(1, len(parse_checklist_items(checklist)))
    max_eval_tokens = min(MAX_CHANGE_EVAL_TOKENS, max(768, 512 + item_count * 100))

    final = {"valid": False, "completed": False, "missing": [], "missing_ids": [], "done_ids": [], "raw": ""}
    for attempt in range(1, MAX_CHANGE_EVAL_RETRIES + 1):
        raw = worker._run_lean_inference(TaskType.REVIEW_CHAPTER, system, user, max_tokens=max_eval_tokens)
        parsed = parse_checklist_eval_result(raw, checklist)
        if parsed["valid"]:
            final = parsed
            logger.info(
                "[write_chapter] Attempt %d/%d: complete=%s done=%s missing_ids=%s",
                attempt, MAX_CHANGE_EVAL_RETRIES, parsed["completed"], parsed.get("done_ids", []), parsed.get("missing_ids", []),
            )
            break
        logger.warning(
            "[write_chapter] Attempt %d/%d returned invalid/unparseable result: %r",
            attempt, MAX_CHANGE_EVAL_RETRIES, (raw or "").strip()[:200],
        )

    if final["valid"]:
        if final["completed"]:
            worker.step_started.emit(f"Write Chapter verification — COMPLETE (pass {pass_number})")
        else:
            missing_text = "\n".join(f"- {item}" for item in final["missing"])
            worker.step_started.emit(
                f"Write Chapter verification — INCOMPLETE (pass {pass_number})\n"
                f"Missing:\n{missing_text or '- evaluator did not identify specific items'}"
            )
    else:
        worker.step_started.emit(
            f"Write Chapter verification — PARSE FAILURE (pass {pass_number}); "
            f"evaluator did not return valid JSON after {MAX_CHANGE_EVAL_RETRIES} attempts."
        )
    return final
