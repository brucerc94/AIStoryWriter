"""Frontier consensus evaluator used by Write Chapter and Change Chapter.

The evaluator returns one ordered progress frontier instead of a free-form list
of done IDs. Python remains the source of truth for checklist state.
"""

from __future__ import annotations

import json
import logging

from engine.models import TaskType

logger = logging.getLogger("workflow")


def _parse_frontier(raw: str, active_items: list[tuple[int, str]]) -> dict:
    active_ids = {item_id for item_id, _ in active_items}
    text = (raw or "").strip()
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object")
        data = json.loads(text[start:end + 1])
        value = data.get("furthest_completed_id")
        frontier = None if value in (None, "", "null") else int(value)
        if frontier is not None and frontier not in active_ids:
            raise ValueError(f"frontier {frontier} is not an active checklist ID")
        return {
            "valid": True,
            "frontier_id": frontier,
            "truncated": bool(data.get("truncated", False)),
            "raw": text,
        }
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.debug("[frontier_eval] invalid evaluator output: %s", exc)
        return {
            "valid": False,
            "frontier_id": None,
            "truncated": False,
            "raw": text,
        }


def _build_eval_prompt(kind: str, chapter_num: int, chapter_title: str, chapter_text: str, active_items: list[tuple[int, str]]) -> tuple[str, str]:
    item_block = "\n".join(f"{item_id}. {text}" for item_id, text in active_items) or "(none)"
    system = (
        "You are a strict narrative-progress evaluator. The checklist is an ORDERED "
        "sequence of story beats. Determine the single furthest checklist ID that the "
        "CURRENT CHAPTER TEXT has actually completed in order by its END. A later item "
        "does not count if it only appears earlier, as foreshadowing, flashback, summary, "
        "dialogue reference, or out of order. Do not infer from any outside context. "
        "Return ONLY JSON in this exact shape: "
        '{"furthest_completed_id": N, "truncated": false}. '
        "Use null for N if no active item is actually completed. If the chapter is "
        "truncated/cut off, set truncated=true."
    )
    user = (
        f"{kind} — Chapter {chapter_num}: {chapter_title}\n\n"
        "ACTIVE CHECKLIST ITEMS (ordered):\n"
        f"{item_block}\n\n"
        "CURRENT CHAPTER TEXT:\n"
        f"{chapter_text}\n\n"
        "Return only the JSON object. Be conservative: when uncertain between two "
        "frontiers, choose the earlier one."
    )
    return system, user


def _vote(worker, system: str, user: str, active_items: list[tuple[int, str]], label: str) -> dict:
    raw = worker._run_lean_inference(TaskType.REVIEW_CHAPTER, system, user, max_tokens=64)
    parsed = _parse_frontier(raw, active_items)
    logger.info(
        "[frontier_eval] %s: frontier=%s truncated=%s valid=%s",
        label, parsed.get("frontier_id"), parsed.get("truncated", False), parsed.get("valid"),
    )
    return parsed


def _consensus(worker, kind: str, chapter_num: int, chapter_title: str, chapter_text: str, active_items: list[tuple[int, str]]) -> tuple[int | None, bool, str, bool]:
    system, user = _build_eval_prompt(kind, chapter_num, chapter_title, chapter_text, active_items)
    second_system = system + (
        " SECOND INDEPENDENT EVALUATION: ignore any presumed answer and re-read the "
        "chapter endpoint from scratch. Do not reward mention of future beats."
    )

    vote_a = _vote(worker, system, user, active_items, f"{kind} evaluator A")
    vote_b = _vote(worker, second_system, user, active_items, f"{kind} evaluator B")
    votes = [v for v in (vote_a, vote_b) if v.get("valid")]

    if not votes:
        return None, False, "", False

    if vote_a.get("valid") and vote_b.get("valid") and vote_a.get("frontier_id") == vote_b.get("frontier_id"):
        chosen = vote_a.get("frontier_id")
    else:
        third_system = system + (
            " THIRD INDEPENDENT TIE-BREAK: determine the furthest ordered checklist "
            "step actually completed at the END of the chapter. Choose the earlier "
            "step whenever the evidence is ambiguous."
        )
        vote_c = _vote(worker, third_system, user, active_items, f"{kind} evaluator C")
        if vote_c.get("valid"):
            votes.append(vote_c)

        counts: dict[int | None, int] = {}
        for vote in votes:
            frontier = vote.get("frontier_id")
            counts[frontier] = counts.get(frontier, 0) + 1
        best = max(counts.values())
        candidates = [frontier for frontier, count in counts.items() if count == best]
        order = {None: -1, **{item_id: idx for idx, (item_id, _) in enumerate(active_items)}}
        chosen = min(candidates, key=lambda value: order.get(value, 10**9))

    chosen_votes = [v for v in votes if v.get("frontier_id") == chosen]
    truncated = any(v.get("truncated", False) for v in chosen_votes)
    raw = " | ".join(v.get("raw", "") for v in votes)
    logger.info("[frontier_eval] %s consensus: votes=%s chosen=%s truncated=%s", kind, [v.get("frontier_id") for v in votes], chosen, truncated)
    return chosen, truncated, raw, True


def _make_result(module, checklist: str, accumulated_done: set[int], active_items: list[tuple[int, str]], frontier: int | None, truncated: bool, raw: str, chapter_text: str) -> dict:
    expected = dict(module.parse_checklist_items(checklist))
    active_ids = [item_id for item_id, _ in active_items]
    if frontier is None:
        count = 0
    else:
        count = active_ids.index(frontier) + 1
    done_ids = active_ids[:count]
    missing_ids = active_ids[count:]
    missing = [f"{item_id}: {dict(active_items)[item_id]}" for item_id in missing_ids]

    all_confirmed = set(accumulated_done) | set(done_ids)
    completed = bool(expected) and set(expected).issubset(all_confirmed) and not missing_ids and not truncated

    if truncated:
        missing.append("Chapter appears truncated.")
        missing_ids.append(0)
        completed = False

    if not completed and not missing:
        missing = [f"{item_id}: {dict(active_items)[item_id]}" for item_id in missing_ids]

    return {
        "valid": True,
        "completed": completed,
        "frontier_id": frontier,
        "truncated": truncated,
        "missing": missing,
        "missing_ids": sorted(set(missing_ids)),
        "done_ids": done_ids,
        "raw": raw,
    }


def _evaluate(module, worker, kind: str, chapter_num: int, chapter_title: str, chapter_text: str, checklist: str, accumulated_done: set[int] | None) -> dict:
    accumulated = set(accumulated_done or ())
    active_items = module._get_active_checklist_items(checklist, accumulated)

    if not active_items:
        truncated = module.ends_abruptly(chapter_text)
        expected = dict(module.parse_checklist_items(checklist))
        return {
            "valid": True,
            "completed": bool(expected) and set(expected).issubset(accumulated) and not truncated,
            "frontier_id": None,
            "truncated": truncated,
            "missing": ["Chapter appears truncated."] if truncated else [],
            "missing_ids": [0] if truncated else [],
            "done_ids": [],
            "raw": "",
        }

    frontier, truncated, raw, valid = _consensus(
        worker, kind, chapter_num, chapter_title, chapter_text, active_items
    )
    if not valid:
        return {
            "valid": False,
            "completed": False,
            "frontier_id": None,
            "truncated": False,
            "missing": [],
            "missing_ids": [],
            "done_ids": [],
            "raw": raw,
        }
    return _make_result(module, checklist, accumulated, active_items, frontier, truncated, raw, chapter_text)


def evaluate_change(worker, chapter_num: int, chapter_content: str, checklist: str, pass_number: int, accumulated_done: set[int] | None = None) -> dict:
    chapter_title = next((c.title for c in worker.project.chapters if c.number == chapter_num), f"Chapter {chapter_num}")
    result = _evaluate(__import__("engine.change_chapter", fromlist=["*"]), worker, "Change Chapter", chapter_num, chapter_title, chapter_content, checklist, accumulated_done)
    logger.info("[change_chapter] Frontier evaluator pass %d: frontier=%s done=%s missing=%s complete=%s", pass_number, result.get("frontier_id"), result.get("done_ids", []), result.get("missing_ids", []), result.get("completed", False))
    return result


def evaluate_chapter(worker, chapter_num: int, chapter_title: str, chapter_content: str, checklist: str, pass_number: int, accumulated_done: set[int] | None = None) -> dict:
    result = _evaluate(__import__("engine.change_chapter", fromlist=["*"]), worker, "Write Chapter", chapter_num, chapter_title, chapter_content, checklist, accumulated_done)
    logger.info("[write_chapter] Frontier evaluator pass %d: frontier=%s done=%s missing=%s complete=%s", pass_number, result.get("frontier_id"), result.get("done_ids", []), result.get("missing_ids", []), result.get("completed", False))
    return result


def install(module) -> None:
    if getattr(module, "_frontier_consensus_installed", False):
        return
    module.evaluate_change = evaluate_change
    module.evaluate_chapter = evaluate_chapter
    module._frontier_consensus_installed = True
    logger.info("[frontier_eval] Installed frontier consensus evaluator for Write/Change Chapter")
