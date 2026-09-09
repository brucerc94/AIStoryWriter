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
from engine.models import TaskType

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
TAIL_LIMIT = 2500




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






def _build_remaining_checklist(checklist: str, completed_ids: set[int] | None = None) -> str:
    completed = set(completed_ids or ())
    remaining = [
        f"{item_id}. {text}"
        for item_id, text in parse_checklist_items(checklist)
        if item_id not in completed
    ]
    return "\n".join(remaining) or "(none)"


def _get_active_checklist_items(checklist: str, completed_ids: set[int]) -> list[tuple[int, str]]:
    """Return only the checklist items not yet confirmed complete — the active window."""
    return [
        (item_id, text)
        for item_id, text in parse_checklist_items(checklist)
        if item_id not in completed_ids
    ]


def _get_next_required_id(checklist: str, completed_ids: set[int]) -> int | None:
    """Return the lowest-numbered item ID that is not yet confirmed complete."""
    for item_id, _ in parse_checklist_items(checklist):
        if item_id not in completed_ids:
            return item_id
    return None


def _format_completed_label(completed_ids: set[int], checklist: str) -> str:
    """Human-readable label for what has been confirmed complete so far."""
    if not completed_ids:
        return "(none yet)"
    all_ids = [item_id for item_id, _ in parse_checklist_items(checklist)]
    done_real = sorted(c for c in completed_ids if c != 0)  # exclude truncation sentinel
    if not done_real:
        return "(none yet)"
    if len(done_real) == 1:
        return str(done_real[0])
    return f"{done_real[0]}..{done_real[-1]} ({len(done_real)} items)"


def _reconcile_active_window_result(
    result: dict,
    checklist: str,
    accumulated_done: set[int],
    active_items: list[tuple[int, str]],
) -> dict:
    """
    Reconcile the evaluator's result for the ACTIVE WINDOW only.

    The evaluator only saw active_items (i.e. the items not yet confirmed by
    accumulated_done). Python state (accumulated_done) tracks what was
    already confirmed in prior passes.

    IMPORTANT — narrative order is enforced here in Python, not merely
    requested in the prompt: an LLM evaluator can mark a later requirement
    "done" even though the chapter text never actually reached it in
    sequence (foreshadowing, miscounting, etc.). We do NOT trust
    `done_ids` at face value. `done` is only accepted as a CONTIGUOUS
    PREFIX of the active IDs in ascending/narrative order, starting from
    the first active ID. The first active ID the evaluator did not mark
    done breaks the sequence; every ID from that point on — even one the
    evaluator explicitly claimed as done — is demoted back to missing.

    Returns a result dict updated with:
      done_ids   – IDs confirmed complete in this window (contiguous prefix only)
      missing_ids – IDs still incomplete in this window
      missing    – human-readable missing requirement strings
      completed  – True only if all checklist items (full list) are now done
    """
    all_expected = dict(parse_checklist_items(checklist))
    active_expected = dict(active_items)
    active_ids = set(active_expected)
    ordered_active_ids = sorted(active_ids)

    # Evaluator's raw output for the active window.
    evaluator_done = set(result.get("done_ids", []))
    evaluator_missing_ids = set(result.get("missing_ids", []))

    # Only consider evaluator verdicts for active IDs; ignore any stray IDs.
    raw_active_done = evaluator_done & active_ids
    active_missing_ids = evaluator_missing_ids & active_ids

    # Enforce the contiguous narrative-order prefix. Anything the evaluator
    # claimed done AFTER the first gap is demoted — the code, not the
    # model, owns the real progress frontier.
    active_done: set[int] = set()
    demoted: set[int] = set()
    broke_sequence = False
    for item_id in ordered_active_ids:
        if not broke_sequence and item_id in raw_active_done:
            active_done.add(item_id)
        else:
            broke_sequence = True
            if item_id in raw_active_done:
                demoted.add(item_id)

    # IDs in the active window the evaluator didn't mention, or that were
    # demoted for breaking narrative order → treated as missing.
    unaccounted = (active_ids - active_done - active_missing_ids) | demoted
    for item_id in sorted(unaccounted):
        active_missing_ids.add(item_id)

    # Rebuild missing list: take evaluator entries for active IDs, add unaccounted/demoted.
    filtered_missing: list[str] = []
    seen_missing: set[str] = set()
    for entry in result.get("missing", []) or []:
        m = re.match(r"^\s*(\d+)\s*:", str(entry))
        if m and int(m.group(1)) not in active_ids and int(m.group(1)) != 0:
            continue  # drop entries for non-active IDs
        text = str(entry).strip()
        if text and text not in seen_missing:
            filtered_missing.append(text)
            seen_missing.add(text)

    for item_id in sorted(unaccounted):
        if item_id in demoted:
            label = (
                f"{item_id}: evaluator marked complete out of narrative order — "
                f"not accepted until earlier active items are confirmed in sequence"
            )
        else:
            label = f"{item_id}: not evaluated — {active_expected[item_id]}"
        if label not in seen_missing:
            filtered_missing.append(label)
            seen_missing.add(label)

    # Keep truncation sentinel if present.
    has_truncation = 0 in evaluator_missing_ids or any(
        "truncat" in str(e).lower() for e in (result.get("missing", []) or [])
    )
    if has_truncation and "Chapter appears truncated." not in seen_missing:
        filtered_missing.append("Chapter appears truncated.")
        active_missing_ids.add(0)

    # Contradictions in the active window: evaluator said both done and missing.
    contradictory = active_done & active_missing_ids
    for item_id in sorted(contradictory):
        active_done.discard(item_id)
        filtered_missing.append(f"Evaluator contradiction on ID {item_id} — treating as missing.")

    # Full picture: accumulated prior + what the evaluator just confirmed.
    all_confirmed = accumulated_done | active_done

    # A chapter is complete when every non-zero item in the full checklist is confirmed.
    all_complete = bool(all_expected) and set(all_expected).issubset(all_confirmed) and not active_missing_ids

    result["done_ids"] = sorted(active_done)
    result["missing_ids"] = sorted(active_missing_ids)
    result["missing"] = filtered_missing
    result["completed"] = all_complete
    return result

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


def _build_eval_prompt(
    worker,
    chapter_num: int,
    chapter_content: str,
    checklist: str,
    accumulated_done: set[int] | None = None,
) -> tuple[str, str, list[tuple[int, str]]]:
    """Build evaluator prompt for the ACTIVE WINDOW (items not yet confirmed complete).

    The evaluator receives ONLY the current chapter text and the active
    checklist — no author request/instruction, no full canon, no author
    style, no chat history. That is the entire evaluation contract: "which
    active checklist requirements does the current text actually satisfy?"

    Returns (system, user, active_items) so the caller has the active item list
    for reconciliation without re-parsing.
    """
    accumulated = set(accumulated_done or ())
    active_items = _get_active_checklist_items(checklist, accumulated)

    if active_items:
        item_block = "\n".join(f"{item_id}. {text}" for item_id, text in active_items)
        id_list = ", ".join(str(item_id) for item_id, _ in active_items)
    else:
        item_block = "(No remaining checklist items; verify chapter ends naturally and is not truncated.)"
        id_list = "(none)"

    system = prompts.render("change_chapter/eval_system", id_list=id_list)
    user = prompts.render(
        "change_chapter/eval_user",
        chapter_number=chapter_num,
        chapter_title=_chapter_title(worker, chapter_num),
        checklist_items=item_block,
        chapter_content=chapter_content,
        id_list=id_list,
    )
    return system, user, active_items


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

            completed = all_items_complete if expected_items else (model_said_complete and not missing_ids and not missing)

            return {
                "valid": True, "completed": completed, "missing": missing,
                "missing_ids": sorted(missing_ids), "done_ids": sorted(done_ids), "raw": raw,
            }
    except Exception as exc:
        logger.debug("[change_chapter] Could not parse evaluator JSON: %s", exc)

    return {"valid": False, "completed": False, "missing": [], "missing_ids": [], "done_ids": [], "raw": raw}


def evaluate_change(
    worker,
    chapter_num: int,
    chapter_content: str,
    checklist: str,
    pass_number: int,
    accumulated_done: set[int] | None = None,
) -> dict:
    """Run the evaluator with retries against the ACTIVE WINDOW only.

    The evaluator receives only the items that are not yet confirmed complete
    (accumulated_done). Python state owns what was confirmed in prior passes —
    the evaluator is never told to 'trust' prior IDs. It also never receives
    the author's original change request — the checklist already encodes the
    intent, and the evaluator's only job is to check current text against it.

    Returns valid=False if every attempt fails to produce parseable JSON.
    """
    accumulated = set(accumulated_done or ())
    system, user, active_items = _build_eval_prompt(
        worker, chapter_num, chapter_content, checklist, accumulated
    )
    active_count = len(active_items)
    logger.info(
        "[change_chapter] Chapter %d — verification pass %d — %d words — "
        "active window: %d item(s) remaining.",
        chapter_num, pass_number, len(chapter_content.split()), active_count,
    )

    # Token budget: scale to the number of ACTIVE items, not the full list.
    max_eval_tokens = min(MAX_CHANGE_EVAL_TOKENS, max(768, 512 + active_count * 100))

    # Build a temporary checklist string containing only active items so
    # parse_checklist_eval_result validates IDs against the active window.
    active_checklist = "\n".join(f"{iid}. {txt}" for iid, txt in active_items)

    final = {"valid": False, "completed": False, "missing": [], "missing_ids": [], "done_ids": [], "raw": ""}
    for attempt in range(1, MAX_CHANGE_EVAL_RETRIES + 1):
        raw = worker._run_lean_inference(TaskType.REVIEW_CHAPTER, system, user, max_tokens=max_eval_tokens)
        # Parse against the active checklist so only active IDs are validated.
        parsed = parse_checklist_eval_result(raw, active_checklist)
        if parsed["valid"]:
            final = _reconcile_active_window_result(parsed, checklist, accumulated, active_items)
            logger.info(
                "[change_chapter] Attempt %d/%d: complete=%s active_done=%s active_missing=%s",
                attempt, MAX_CHANGE_EVAL_RETRIES,
                final["completed"], final.get("done_ids", []), final.get("missing_ids", []),
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


def _build_continuation_prompt(
    worker,
    chapter_num: int,
    chapter_content: str,
    checklist: str,
    missing: list[str] | None,
    completed_ids: set[int] | None = None,
    remaining_checklist: str = "",
) -> tuple[str, str]:
    """
    Build the system + user prompt for a Change Chapter continuation pass.

    RESET: this is a brand-new, self-contained model call. It does NOT
    receive the author's original USER CHANGE REQUEST — that request was
    already converted into the (immutable) checklist during planning, and
    re-supplying it here risks the model reinterpreting the original intent
    instead of simply picking up from the current narrative position. The
    checklist is the sole authoritative specification of what remains to be
    done.

    The continuation prompt does NOT include the full checklist either. It
    only shows the active (remaining) requirements so the model focuses on
    what is actually still needed. Completed requirements are referenced by a
    human-readable progress label managed entirely by Python state.

    It also does NOT display the chapter's outline/plan text as continuity
    context — only the active checklist and the exact prose tail drive what
    comes next, so the model can't jump ahead to future beats it finds in
    the outline. The outline text is still used, internally and silently,
    to help select which characters/world entries are relevant.

    Uses a dynamic budget so continuity anchors are never silently truncated
    before the recent prose tail.
    """
    # ------------------------------------------------------------------
    # Dynamic budget
    # ------------------------------------------------------------------
    CHARS_PER_TOKEN = 4
    FIXED_OVERHEAD_TOKENS = 700   # system prompt + template skeleton + instruction
    try:
        ctx_tokens = worker._model_context_limit()
    except Exception:
        ctx_tokens = 4096
    reply_tokens = worker._content_max_tokens()
    prompt_token_budget = max(256, ctx_tokens - reply_tokens - FIXED_OVERHEAD_TOKENS)
    total_chars = prompt_token_budget * CHARS_PER_TOKEN

    missing_text = "\n".join(f"- {item}" for item in (missing or [])) or "(none identified; continue satisfying the active checklist)"
    language = worker._response_language()

    accumulated = set(completed_ids or ())
    active_remaining = remaining_checklist.strip() or _build_remaining_checklist(checklist, accumulated)
    last_completed_label = _format_completed_label(accumulated, checklist)
    next_id = _get_next_required_id(checklist, accumulated)
    next_required_label = str(next_id) if next_id is not None else "(all items pending or complete)"

    project = worker.project
    # The outline/chapter-plan text is used ONLY to help select relevant
    # canon below — it is deliberately NOT displayed to the model in this
    # continuation prompt. Showing a chapter's full plan here would expose
    # future beats not yet reached and risks the model jumping ahead; the
    # only forward-looking instruction a continuation should see is the
    # ACTIVE/REMAINING checklist and its NEXT REQUIRED ITEM.
    outline_raw    = (extract_outline_section(project.outline, chapter_num) or "").strip()
    prose_tail_raw = chapter_content[-TAIL_LIMIT:].strip()
    selection_source = "\n\n".join(
        part for part in (outline_raw, active_remaining, missing_text, prose_tail_raw) if part
    )
    characters_raw, world_raw = build_relevant_chapter_context(
        project, selection_source, max_character_chars=5000, max_world_chars=6000
    )

    # Fixed text that doesn't participate in budget allocation.
    # Note: we use active_remaining instead of the full checklist here.
    fixed_chars = len(active_remaining) + len(missing_text)
    variable_budget = max(400, total_chars - fixed_chars)

    # Priority order: characters > world > prose tail. The outline/chapter
    # plan is intentionally excluded from the displayed slots — see above.
    slots = [
        # (name,         full_text,      min_chars)
        ("characters",  characters_raw, min(len(characters_raw), 600)),
        ("world",       world_raw,      min(len(world_raw),      400)),
        ("prose_tail",  prose_tail_raw, min(len(prose_tail_raw), 1600)),
    ]
    allocated = budget_allocate(variable_budget, slots)

    continuity_context = prompts.render(
        "change_chapter/section",
        heading="CONTINUITY ANCHORS — AUTHORITATIVE",
        body=(
            f"Established characters:\n{allocated['characters']}\n\n"
            f"World/setting anchors:\n{allocated['world']}\n\n"
        ),
    )

    logger.debug(
        "[change_chapter] continuation budget: ctx=%d reply=%d prompt_budget=%d chars "
        "chars=%d world=%d tail=%d active_remaining=%d",
        ctx_tokens, reply_tokens, total_chars,
        len(allocated["characters"]),
        len(allocated["world"]), len(allocated["prose_tail"]),
        len(active_remaining),
    )

    # AUTHOR STYLE is reconstructed from project.writing_style on every
    # RESET pass — it must be present in every prose-generating call.
    style_frag = worker.project.writing_style.to_prompt_fragment()
    system = prompts.render(
        "change_chapter/continue_system",
        language_note=f" Continue in {language}." if language else "",
        style_block=f"\n\nWriting style to preserve:\n{style_frag}" if style_frag else "",
    )
    user = prompts.render(
        "change_chapter/continue_user",
        chapter_number=chapter_num,
        chapter_title=_chapter_title(worker, chapter_num),
        last_completed_id_label=last_completed_label,
        remaining_checklist=active_remaining,
        next_required_id_label=next_required_label,
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


def continue_rewrite(worker, chapter_num: int, chapter, checklist: str, pass_number: int, missing: list[str] | None = None, completed_ids: set[int] | None = None, remaining_checklist: str = "") -> bool:
    """Append a continuation to chapter.content. Returns True if new content was appended.

    RESET pass: does not receive the original USER CHANGE REQUEST — see
    _build_continuation_prompt for why.
    """
    worker.step_started.emit(f"Continuing Chapter {chapter_num} rewrite (pass {pass_number})...")
    system, user = _build_continuation_prompt(worker, chapter_num, chapter.content, checklist, missing, completed_ids, remaining_checklist)
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

    # RESET architecture: Change Chapter never reads from or writes to
    # project.chat_messages. Nothing about this rewrite is added to the
    # conversational chat history — the UI does not need it, and doing so
    # would risk chat history leaking into later, unrelated inference calls.

    consecutive_invalid_evals = 0
    # accumulated_done tracks the full set of confirmed IDs across all passes.
    # It is NEVER sent to the evaluator as a hint — only the code uses it.
    accumulated_done: set[int] = set()
    for pass_number in range(1, MAX_CHANGE_PASSES + 1):
        evaluation = evaluate_change(
            worker, chapter_num, chapter.content, checklist,
            pass_number, accumulated_done,
        )

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

        # Accumulate newly confirmed IDs into Python state.
        newly_done = set(evaluation.get("done_ids", []))
        accumulated_done.update(newly_done)
        remaining_checklist = _build_remaining_checklist(checklist, accumulated_done)
        logger.info(
            "[change_chapter] Pass %d: newly_done=%s accumulated_done=%s remaining=%d item(s).",
            pass_number, sorted(newly_done), sorted(accumulated_done),
            len(_get_active_checklist_items(checklist, accumulated_done)),
        )

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

        continued = continue_rewrite(
            worker, chapter_num, chapter, checklist,
            pass_number + 1, evaluation.get("missing", []),
            accumulated_done, remaining_checklist,
        )
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
    chapter_num: int,
    chapter_title: str,
    chapter_content: str,
    checklist: str,
    accumulated_done: set[int] | None = None,
) -> tuple[str, str, list[tuple[int, str]]]:
    """Build write_chapter evaluator prompt for the ACTIVE WINDOW only.

    The evaluator receives ONLY the current chapter text and the active
    checklist — no outline, no canon, no author style, no chat history.
    Returns (system, user, active_items). The evaluator never sees items
    already confirmed complete — those are tracked by Python state only.
    """
    accumulated = set(accumulated_done or ())
    active_items = _get_active_checklist_items(checklist, accumulated)

    if active_items:
        item_block = "\n".join(f"{item_id}. {text}" for item_id, text in active_items)
        id_list = ", ".join(str(item_id) for item_id, _ in active_items)
    else:
        item_block = "(No remaining checklist items; verify chapter ends naturally and is not truncated.)"
        id_list = "(none)"

    system = prompts.render("write_chapter/eval_system", id_list=id_list)
    user = prompts.render(
        "write_chapter/eval_user",
        chapter_number=chapter_num,
        chapter_title=chapter_title,
        checklist_items=item_block,
        chapter_content=chapter_content,
        id_list=id_list,
    )
    return system, user, active_items


def evaluate_chapter(
    worker,
    chapter_num: int,
    chapter_title: str,
    chapter_content: str,
    checklist: str,
    pass_number: int,
    accumulated_done: set[int] | None = None,
) -> dict:
    """Run the WRITE_CHAPTER checklist evaluator against the ACTIVE WINDOW only.

    The evaluator receives only the items not yet confirmed complete
    (accumulated_done). Python state tracks what was confirmed in prior passes —
    the evaluator is never told to 'trust' prior IDs. It never receives the
    outline, project canon, author style, or chat history — only the current
    chapter text and the active checklist.

    Returns valid=False if every attempt fails to produce parseable JSON.
    """
    accumulated = set(accumulated_done or ())
    system, user, active_items = _build_chapter_eval_prompt(
        chapter_num, chapter_title, chapter_content, checklist, accumulated
    )
    active_count = len(active_items)
    logger.info(
        "[write_chapter] Chapter %d — checklist verification pass %d — %d words — "
        "active window: %d item(s) remaining.",
        chapter_num, pass_number, len(chapter_content.split()), active_count,
    )

    # Token budget scaled to active item count.
    max_eval_tokens = min(MAX_CHANGE_EVAL_TOKENS, max(768, 512 + active_count * 100))

    # Build a temporary checklist of active items only for parse validation.
    active_checklist = "\n".join(f"{iid}. {txt}" for iid, txt in active_items)

    final = {"valid": False, "completed": False, "missing": [], "missing_ids": [], "done_ids": [], "raw": ""}
    for attempt in range(1, MAX_CHANGE_EVAL_RETRIES + 1):
        raw = worker._run_lean_inference(TaskType.REVIEW_CHAPTER, system, user, max_tokens=max_eval_tokens)
        parsed = parse_checklist_eval_result(raw, active_checklist)
        if parsed["valid"]:
            final = _reconcile_active_window_result(parsed, checklist, accumulated, active_items)
            logger.info(
                "[write_chapter] Attempt %d/%d: complete=%s active_done=%s active_missing=%s",
                attempt, MAX_CHANGE_EVAL_RETRIES,
                final["completed"], final.get("done_ids", []), final.get("missing_ids", []),
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
