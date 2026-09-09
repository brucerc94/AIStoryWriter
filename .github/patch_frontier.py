from pathlib import Path
import ast
import subprocess


ROOT = Path(__file__).resolve().parents[1]


FRONTIER_BLOCK = '''def parse_checklist_eval_result(text: str, checklist: str) -> dict:
    """Parse compact frontier-only evaluator output."""
    raw = (text or "").strip()
    expected = dict(parse_checklist_items(checklist))
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object")
        data = json.loads(raw[start:end + 1])
        frontier_raw = data.get("furthest_completed_id")
        frontier = None if frontier_raw in (None, "", "null") else int(frontier_raw)
        if frontier is not None and frontier not in expected:
            raise ValueError(f"frontier {frontier} is not an active checklist ID")
        return {
            "valid": True,
            "completed": False,
            "frontier_id": frontier,
            "truncated": bool(data.get("truncated", False)),
            "missing": [],
            "missing_ids": [],
            "done_ids": [],
            "raw": raw,
        }
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.debug("[change_chapter] Could not parse frontier evaluator JSON: %s", exc)
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


def _result_from_frontier(frontier: int | None, truncated: bool, checklist: str, accumulated_done: set[int], active_items: list[tuple[int, str]]) -> dict:
    active_ids = [item_id for item_id, _ in active_items]
    active_expected = dict(active_items)
    prefix_count = 0 if frontier is None else active_ids.index(frontier) + 1
    done_ids = active_ids[:prefix_count]
    missing_ids = active_ids[prefix_count:]
    missing = [f"{item_id}: {active_expected[item_id]}" for item_id in missing_ids]
    all_expected = {item_id for item_id, _ in parse_checklist_items(checklist)}
    all_confirmed = set(accumulated_done) | set(done_ids)
    completed = bool(all_expected) and all_expected.issubset(all_confirmed) and not missing_ids and not truncated
    if truncated:
        missing.append("Chapter appears truncated.")
        missing_ids = list(missing_ids) + [0]
    return {
        "valid": True,
        "completed": completed,
        "frontier_id": frontier,
        "truncated": truncated,
        "missing": missing,
        "missing_ids": sorted(set(missing_ids)),
        "done_ids": done_ids,
        "raw": "",
    }


def _frontier_vote(worker, system: str, user: str, active_checklist: str, label: str) -> dict:
    raw = worker._run_lean_inference(TaskType.REVIEW_CHAPTER, system, user, max_tokens=64)
    parsed = parse_checklist_eval_result(raw, active_checklist)
    if parsed["valid"]:
        logger.info("[change_chapter] %s frontier=%s truncated=%s", label, parsed.get("frontier_id"), parsed.get("truncated", False))
    else:
        logger.warning("[change_chapter] %s returned invalid frontier JSON: %r", label, (raw or "").strip()[:200])
    return parsed


def _evaluate_frontier_consensus(worker, system: str, user: str, active_items: list[tuple[int, str]], active_checklist: str, checklist: str, accumulated_done: set[int], label: str) -> dict:
    if not active_items:
        return _result_from_frontier(None, False, checklist, accumulated_done, active_items)

    system_b = system + "\\n\\nSECOND INDEPENDENT EVALUATION: re-read the chapter endpoint from scratch. Be skeptical and conservative. Identify only the furthest ordered checklist step actually reached."
    vote_a = _frontier_vote(worker, system, user, active_checklist, f"{label} evaluator A")
    vote_b = _frontier_vote(worker, system_b, user, active_checklist, f"{label} evaluator B")
    votes = [v for v in (vote_a, vote_b) if v.get("valid")]

    if len(votes) < 2 or vote_a.get("frontier_id") != vote_b.get("frontier_id"):
        system_c = system + "\\n\\nTHIRD INDEPENDENT TIE-BREAK EVALUATION: determine the furthest ordered checklist step actually reached at the chapter endpoint. Do not infer later progress. When uncertain, choose the earlier step."
        vote_c = _frontier_vote(worker, system_c, user, active_checklist, f"{label} evaluator C")
        if vote_c.get("valid"):
            votes.append(vote_c)

    if not votes:
        return {"valid": False, "completed": False, "frontier_id": None, "truncated": False, "missing": [], "missing_ids": [], "done_ids": [], "raw": ""}

    frontiers = [v.get("frontier_id") for v in votes]
    counts = {}
    for frontier in frontiers:
        counts[frontier] = counts.get(frontier, 0) + 1
    best_count = max(counts.values())
    candidates = [frontier for frontier, count in counts.items() if count == best_count]

    order = {None: -1, **{item_id: idx for idx, (item_id, _) in enumerate(active_items)}}
    chosen = min(candidates, key=lambda value: order.get(value, 10**9))
    truncated = any(v.get("truncated", False) for v in votes if v.get("frontier_id") == chosen)
    result = _result_from_frontier(chosen, truncated, checklist, accumulated_done, active_items)
    result["raw"] = " | ".join(v.get("raw", "") for v in votes)
    logger.info("[change_chapter] %s frontier consensus: votes=%s chosen=%s truncated=%s", label, frontiers, chosen, truncated)
    return result
'''


EVAL_CHANGE = '''def evaluate_change(
    worker,
    chapter_num: int,
    chapter_content: str,
    checklist: str,
    pass_number: int,
    accumulated_done: set[int] | None = None,
) -> dict:
    accumulated = set(accumulated_done or ())
    system, user, active_items = _build_eval_prompt(worker, chapter_num, chapter_content, checklist, accumulated)
    active_checklist = "\\n".join(f"{iid}. {txt}" for iid, txt in active_items)
    final = _evaluate_frontier_consensus(
        worker, system, user, active_items, active_checklist, checklist,
        accumulated, "Change Chapter",
    )
    if not final["valid"]:
        worker.step_started.emit(
            f"Change Chapter verification — PARSE FAILURE (pass {pass_number}); evaluator returned no valid frontier."
        )
        return final
    if final["completed"]:
        worker.step_started.emit(f"Change Chapter verification — COMPLETE (pass {pass_number})")
    else:
        missing_text = "\\n".join(f"- {item}" for item in final["missing"])
        worker.step_started.emit(
            f"Change Chapter verification — INCOMPLETE (pass {pass_number})\\n"
            f"Missing:\\n{missing_text or '- evaluator did not identify specific items'}"
        )
    return final
'''


EVAL_CHAPTER = '''def evaluate_chapter(
    worker,
    chapter_num: int,
    chapter_title: str,
    chapter_content: str,
    checklist: str,
    pass_number: int,
    accumulated_done: set[int] | None = None,
) -> dict:
    accumulated = set(accumulated_done or ())
    system, user, active_items = _build_chapter_eval_prompt(
        chapter_num, chapter_title, chapter_content, checklist, accumulated
    )
    active_checklist = "\\n".join(f"{iid}. {txt}" for iid, txt in active_items)
    final = _evaluate_frontier_consensus(
        worker, system, user, active_items, active_checklist, checklist,
        accumulated, "Write Chapter",
    )
    if not final["valid"]:
        worker.step_started.emit(
            f"Write Chapter verification — PARSE FAILURE (pass {pass_number}); evaluator returned no valid frontier."
        )
        return final
    if final["completed"]:
        worker.step_started.emit(f"Write Chapter verification — COMPLETE (pass {pass_number})")
    else:
        missing_text = "\\n".join(f"- {item}" for item in final["missing"])
        worker.step_started.emit(
            f"Write Chapter verification — INCOMPLETE (pass {pass_number})\\n"
            f"Missing:\\n{missing_text or '- evaluator did not identify specific items'}"
        )
    return final
'''


def replace_function(text: str, name: str, next_name: str, body: str) -> str:
    start = text.index(f"def {name}(")
    end = text.index(f"\ndef {next_name}(", start)
    return text[:start] + body.rstrip() + "\n" + text[end + 1:]


# Patch change_chapter.py
p = ROOT / "engine" / "change_chapter.py"
s = p.read_text(encoding="utf-8")
start = s.index("def parse_checklist_eval_result(")
end = s.index("\ndef evaluate_change(", start)
s = s[:start] + FRONTIER_BLOCK.rstrip() + "\n\n" + s[end + 1:]
s = replace_function(s, "evaluate_change", "ends_abruptly", EVAL_CHANGE)
a = s.index("def evaluate_chapter(")
b = s.index("\ndef ", a + 5)
s = s[:a] + EVAL_CHAPTER.rstrip() + "\n" + s[b + 1:]
ast.parse(s)
p.write_text(s, encoding="utf-8")

# Patch both evaluator templates.
system = """You are a strict ordered-progress evaluator for a novel chapter. The checklist is an ORDERED NARRATIVE EXECUTION SEQUENCE. Determine the single furthest checklist ID that the CURRENT CHAPTER TEXT has actually reached at its narrative endpoint. Do not report individual done IDs. A later item does NOT count merely because its event or outcome appears earlier, in a flashback, as foreshadowing, in dialogue, in a summary, or out of sequence. Stop at the first active item not actually reached/completed in order. If no active item was reached, use null. If the chapter is truncated or cut off, set truncated=true. Return ONLY compact valid JSON: {\"furthest_completed_id\":N|null,\"truncated\":true|false}. Only the current chapter text establishes completion; do not infer from outline, synopsis, memory, canon, or prior state."""
change_user = """Chapter {{chapter_number}}: '{{chapter_title}}'\n\nACTIVE CHECKLIST ITEMS — ORDERED NARRATIVE STEPS:\n{{checklist_items}}\n\nCURRENT FULL CHAPTER:\n{{chapter_content}}\n\nFind the furthest active checklist ID actually completed in order by the END of the current chapter. Be conservative: when uncertain between two IDs, choose the earlier one. A character, event, detail, or outcome that appears before its required position does not advance the frontier. Return only the required JSON object."""
write_user = """Chapter {{chapter_number}}: '{{chapter_title}}'\n\nACTIVE CHECKLIST ITEMS — ORDERED NARRATIVE STEPS:\n{{checklist_items}}\n\nCURRENT CHAPTER DRAFT:\n{{chapter_content}}\n\nFind the furthest active checklist ID actually completed in order by the END of the current draft. Be conservative: when uncertain between two IDs, choose the earlier one. A character, event, detail, or outcome that appears before its required position does not advance the frontier. Return only the required JSON object."""
for path, content in [
    (ROOT / "engine" / "prompts" / "change_chapter" / "eval_system.txt", system),
    (ROOT / "engine" / "prompts" / "change_chapter" / "eval_user.txt", change_user),
    (ROOT / "engine" / "prompts" / "write_chapter" / "eval_system.txt", system),
    (ROOT / "engine" / "prompts" / "write_chapter" / "eval_user.txt", write_user),
]:
    path.write_text(content, encoding="utf-8")

# Remove temporary patch files from previous attempts.
for rel in [
    ".github/workflows/_apply_frontier_consensus.yml",
    ".github/workflows/z_apply_frontier_consensus.yml",
    ".github/workflows/z2_apply_frontier_consensus.yml",
    ".github/workflows/z3_apply_frontier_consensus.yml",
    ".github/workflows/z2_apply_frontier_consensus.yml",
    ".github/patch_frontier.py",
]:
    path = ROOT / rel
    if path.exists():
        path.unlink()

# Basic source validation without running the application's test suite.
ast.parse((ROOT / "engine" / "change_chapter.py").read_text(encoding="utf-8"))

subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True, cwd=ROOT)
subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True, cwd=ROOT)
subprocess.run(["git", "add", "engine/change_chapter.py", "engine/prompts/change_chapter/eval_system.txt", "engine/prompts/change_chapter/eval_user.txt", "engine/prompts/write_chapter/eval_system.txt", "engine/prompts/write_chapter/eval_user.txt", ".github/workflows", ".github/patch_frontier.py"], check=True, cwd=ROOT)
subprocess.run(["git", "commit", "-m", "fix: frontier consensus evaluator"], check=True, cwd=ROOT)
subprocess.run(["git", "push", "origin", "bugfixes"], check=True, cwd=ROOT)
print("PATCH_AND_PUSH_OK")
