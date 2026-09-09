"""Preflight consistency check for Change Chapter.

This runs before the change-request planner. It only inspects the current
chapter text, detects internal continuity problems, and repairs them when
needed. It deliberately receives no user change request, canon, memory,
outline, author style, or chat history.
"""

from __future__ import annotations

import json
import logging

from engine.models import TaskType

logger = logging.getLogger("workflow")

_CONSISTENCY_EVAL_TOKENS = 384
_MIN_REPAIR_RATIO = 0.70


def _parse_result(raw: str) -> dict:
    text = (raw or "").strip()
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object")
        data = json.loads(text[start : end + 1])
        return {
            "valid": isinstance(data, dict),
            "consistent": bool(data.get("consistent", False)),
            "issues": data.get("issues", []) if isinstance(data.get("issues", []), list) else [],
            "raw": text,
        }
    except (ValueError, TypeError, json.JSONDecodeError):
        return {"valid": False, "consistent": False, "issues": [], "raw": text}


def _issues_text(issues: list[object]) -> str:
    lines: list[str] = []
    for issue in issues[:12]:
        if isinstance(issue, dict):
            kind = str(issue.get("type", "continuity" )).strip()
            desc = str(issue.get("description", issue.get("issue", ""))).strip()
            if desc:
                lines.append(f"- {kind}: {desc}")
        else:
            value = str(issue).strip()
            if value:
                lines.append(f"- {value}")
    return "\n".join(lines) or "- Internal continuity problem detected; repair only clear contradictions."


def _looks_like_wrapped_output(text: str) -> bool:
    stripped = (text or "").strip()
    lowered = stripped.lower()
    return (
        stripped.startswith("```")
        or lowered.startswith("consistent:")
        or lowered.startswith("repaired chapter:")
        or lowered.startswith("here is the corrected")
    )


def evaluate_and_repair(worker, chapter_num: int, chapter_content: str) -> str:
    """Check only internal chapter consistency and minimally repair clear issues."""
    content = chapter_content or ""
    if not content.strip():
        return content

    worker.step_started.emit(f"Evaluating consistency of Chapter {chapter_num}...")

    from engine import prompts

    system = prompts.render("change_chapter/consistency_system")
    user = prompts.render(
        "change_chapter/consistency_user",
        chapter_content=content,
    )
    raw = worker._run_lean_inference(
        TaskType.REVIEW_CHAPTER,
        system,
        user,
        max_tokens=_CONSISTENCY_EVAL_TOKENS,
    )
    result = _parse_result(raw)
    if not result["valid"]:
        logger.warning(
            "[change_chapter] Consistency evaluator returned invalid JSON; keeping original chapter."
        )
        worker.step_started.emit(
            "Consistency check could not be parsed; continuing with the original chapter."
        )
        return content

    issues = result["issues"]
    if result["consistent"] or not issues:
        logger.info("[change_chapter] Consistency check: no internal continuity problems detected.")
        worker.step_started.emit("Chapter consistency check passed.")
        return content

    logger.info("[change_chapter] Consistency check found %d issue(s); repairing before planning change.", len(issues))
    worker.step_started.emit(
        f"Chapter consistency found {len(issues)} issue(s). Repairing before applying the requested change..."
    )

    repair_system = prompts.render("change_chapter/consistency_repair_system")
    repair_user = prompts.render(
        "change_chapter/consistency_repair_user",
        chapter_content=content,
        issues=_issues_text(issues),
    )
    repaired = worker._run_lean_inference(
        TaskType.CHANGE_CHAPTER,
        repair_system,
        repair_user,
        max_tokens=worker._content_max_tokens(),
    )
    repaired = (repaired or "").strip()
    original_len = max(1, len(content.split()))
    repaired_len = len(repaired.split())

    if (
        not repaired
        or _looks_like_wrapped_output(repaired)
        or repaired_len < int(original_len * _MIN_REPAIR_RATIO)
    ):
        logger.warning(
            "[change_chapter] Consistency repair looked unsafe (words %d -> %d); keeping original chapter.",
            original_len,
            repaired_len,
        )
        worker.step_started.emit(
            "Consistency repair was unsafe or incomplete; keeping the original chapter."
        )
        return content

    logger.info(
        "[change_chapter] Consistency repair accepted (words %d -> %d).",
        original_len,
        repaired_len,
    )
    worker.step_started.emit("Chapter consistency repaired; proceeding to Change Chapter planning.")
    return repaired


def install_change_run(module) -> None:
    """Wrap Change Chapter's entry point with the consistency precheck once."""
    if getattr(module, "_consistency_precheck_installed", False):
        return

    original_run = module.run

    def run_with_consistency(worker) -> None:
        chapter_num = worker.project.current_chapter or len(worker.project.chapters)
        chapter = next((c for c in worker.project.chapters if c.number == chapter_num), None)

        if chapter is None or not chapter.content.strip() or not worker.extra_input.strip():
            original_run(worker)
            return

        checked_content = evaluate_and_repair(worker, chapter_num, chapter.content)
        if checked_content and checked_content != chapter.content:
            chapter.content = checked_content

        original_run(worker)

    module.run = run_with_consistency
    module._consistency_precheck_installed = True
    logger.info("[change_chapter] Consistency precheck installed.")
