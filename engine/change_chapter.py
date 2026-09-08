"""
CHANGE_CHAPTER workflow: checklist plan → full rewrite
with clean context → checklist evaluation → continuation until complete.

World is refreshed only once the chapter is accepted.
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
from engine.context import budget_allocate, build_relevant_chapter_context, extract_outline_section
from engine.models import ChatMessage, MessageRole, TaskType

logger = logging.getLogger("workflow")

MAX_CHANGE_EVAL_RETRIES = 3
MAX_CHANGE_PLAN_TOKENS = 2048
MAX_CHANGE_PASSES = 10

MAX_CHANGE_EVAL_TOKENS = 3072

_DUPLICATE_OVERLAP_RATIO = 0.60

MAX_CONSECUTIVE_INVALID_EVALS = 2

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






def build_clean_context_sections(worker, chapter_num: int, selection_source: str = "") -> str:
    """Assemble scoped canon context for the target chapter only."""
    project = worker.project
    chapter_outline = extract_outline_section(project.outline, chapter_num) or "(no outline entry for this chapter)"
    source = "\n\n".join(part for part in (chapter_outline, selection_source) if part)
    characters, world = build_relevant_chapter_context(
        project, source, max_character_chars=5000, max_world_chars=6000
    )

    sections = []
    if characters:
        sections.append(prompts.render(
            "change_chapter/section",
            heading="RELEVANT CHARACTERS (project canon — use only these established records)",
            body=characters,
        ))
    if world:
        sections.append(prompts.render(
            "change_chapter/section",
            heading="RELEVANT WORLD & SETTING (project canon — scoped to this chapter)",
            body=world,
        ))
    intent_frag = project.author_intent.to_prompt_fragment()
    style_frag = project.writing_style.to_prompt_fragment()
    if intent_frag:
        sections.append(prompts.render("change_chapter/section", heading="AUTHOR INTENT", body=intent_frag))
    if style_frag:
        sections.append(prompts.render("change_chapter/section", heading="WRITING STYLE", body=style_frag))
    sections.append(prompts.render("change_chapter/section", heading="CURRENT CHAPTER PLAN", body=chapter_outline))
    return "\n\n".join(sections)

def build_full_rewrite_prompt(
    worker, chapter_num: int, chapter_content: str, instruction: str, checklist: str,
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
        context_sections=build_clean_context_sections(worker, chapter_num, "\n\n".join((instruction, checklist, chapter_content))),
        chapter_content=_cap(chapter_content, CHAPTER_CAP, "part of the chapter"),
    )
    return system, user


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


            unaccounted = set(expected_items) - done_ids - missing_ids
            for item_id in sorted(unaccounted):
                missing_ids.add(item_id)
                missing.append(f"{item_id}: not evaluated — {expected_items[item_id]}")


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






def ends_abruptly(text: str) -> bool:
    stripped = (text or "").rstrip()
    if not stripped:
        return True
    if len(stripped) <= 80:
        return False
    return not bool(re.search(r'[.!?\u2026"\')\]}\u201d\u2019]$', stripped))


def _build_continuation_prompt(worker, chapter_num: int, chapter_content: str, instruction: str, checklist: str, missing: list[str] | None) -> tuple[str, str]:
    """
    Build the system + user prompt for a Change Chapter continuation pass.

    Uses a dynamic budget so continuity anchors are never silently truncated
    before the recent prose tail: the available character budget is derived
    from the live model context size and the configured reply reservation,
    then distributed across slots in priority order via budget_allocate().
    """
    # ------------------------------------------------------------------
    # Dynamic budget
    # ------------------------------------------------------------------
    CHARS_PER_TOKEN = 4
    FIXED_OVERHEAD_TOKENS = 700   # system prompt + template skeleton + instruction/checklist
    try:
        ctx_tokens = worker._model_context_limit()
    except Exception:
        ctx_tokens = 4096
    reply_tokens = worker._content_max_tokens()
    prompt_token_budget = max(256, ctx_tokens - reply_tokens - FIXED_OVERHEAD_TOKENS)
    total_chars = prompt_token_budget * CHARS_PER_TOKEN

    missing_text = "\n".join(f"- {item}" for item in (missing or [])) or "(none identified; verify the original request yourself)"
    language = worker._response_language()

    project = worker.project
    outline_raw    = (extract_outline_section(project.outline, chapter_num) or "").strip() or "(none)"
    prose_tail_raw = chapter_content.strip()
    selection_source = "\n\n".join(
        part for part in (outline_raw, instruction, checklist, missing_text, prose_tail_raw) if part
    )
    characters_raw, world_raw = build_relevant_chapter_context(
        project, selection_source, max_character_chars=5000, max_world_chars=6000
    )

    # Fixed text that doesn't participate in budget allocation.
    fixed_chars = len(instruction) + len(checklist) + len(missing_text)
    variable_budget = max(400, total_chars - fixed_chars)

    # Priority order: outline > characters > world > memory > prose tail.
    # Prose tail gets the largest minimum (scene-state continuity), but
    # anchors always appear first and are never bumped off by the tail.
    slots = [
        # (name,         full_text,      min_chars)
        ("outline",     outline_raw,    min(len(outline_raw),    800)),
        ("characters",  characters_raw, min(len(characters_raw), 600)),
        ("world",       world_raw,      min(len(world_raw),      400)),
        ("prose_tail",  prose_tail_raw, min(len(prose_tail_raw), 1600)),
    ]
    allocated = budget_allocate(variable_budget, slots)

    continuity_context = prompts.render(
        "change_chapter/section",
        heading="CONTINUITY ANCHORS — AUTHORITATIVE",
        body=(
            f"Current chapter plan:\n{allocated['outline']}\n\n"
            f"Established characters:\n{allocated['characters']}\n\n"
            f"World/setting anchors:\n{allocated['world']}\n\n"
        ),
    )

    logger.debug(
        "[change_chapter] continuation budget: ctx=%d reply=%d prompt_budget=%d chars "
        "outline=%d chars=%d world=%d tail=%d",
        ctx_tokens, reply_tokens, total_chars,
        len(allocated["outline"]), len(allocated["characters"]),
        len(allocated["world"]), len(allocated["prose_tail"]),
    )

    system = prompts.render("change_chapter/continue_system", language_note=f" Continue in {language}." if language else "")
    user = prompts.render(
        "change_chapter/continue_user",
        chapter_number=chapter_num,
        chapter_title=_chapter_title(worker, chapter_num),
        instruction=instruction.strip(),
        checklist=checklist.strip() or "(none)",
        missing_text=missing_text,
        continuity_context=continuity_context,
        chapter_tail=allocated["prose_tail"],
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




    worker.step_started.emit(f"Planning Change Chapter {chapter_num}...")
    checklist = plan_change(worker, chapter_num, original_content, instruction)


    worker.step_started.emit(f"Rewriting Chapter {chapter_num} with your changes...")
    system, user = build_full_rewrite_prompt(worker, chapter_num, original_content, instruction, checklist)
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


    worker._finalize_changed_chapter(chapter_num, chapter)
    from engine import storage
    storage.save_project(worker.project)
    worker.step_finished.emit(f"Updated Chapter {chapter_num} (continuation limit reached)", chapter.content)











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
