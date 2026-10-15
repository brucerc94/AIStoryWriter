"""Preflight consistency check for a Change Chapter request.

This runs before the change-request planner. It inspects ONLY the user's
requested change, detects contradictions or impossible sequencing inside that
request, and rewrites the REQUEST minimally when needed. The existing chapter
is not modified during this phase.
"""

from __future__ import annotations

import json
import logging

from engine.models import TaskType

logger = logging.getLogger("workflow")

_CONSISTENCY_EVAL_TOKENS = 384
_MIN_REPAIR_RATIO = 0.60


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
            kind = str(issue.get("type", "continuity")).strip()
            desc = str(issue.get("description", issue.get("issue", ""))).strip()
            if desc:
                lines.append(f"- {kind}: {desc}")
        else:
            value = str(issue).strip()
            if value:
                lines.append(f"- {value}")
    return "\n".join(lines) or "- Clear internal contradiction or sequencing problem detected."


def _looks_like_wrapped_output(text: str) -> bool:
    stripped = (text or "").strip()
    lowered = stripped.lower()
    return (
        stripped.startswith("```")
        or lowered.startswith("consistent:")
        or lowered.startswith("corrected request:")
        or lowered.startswith("here is the corrected")
    )


def evaluate_and_repair_request(worker, change_request: str) -> str:
    """Validate only the user change request and minimally repair contradictions."""
    request = (change_request or "").strip()
    if not request:
        return request

    worker.step_started.emit("Evaluating Change Chapter request consistency...")

    from engine import prompts

    system = prompts.render("change_chapter/consistency_system")
    user = prompts.render(
        "change_chapter/consistency_user",
        change_request=request,
    )
    raw = worker._run_lean_inference(
        TaskType.REVIEW_CHAPTER,
        system,
        user,
        max_tokens=_CONSISTENCY_EVAL_TOKENS,
    )
    result = _parse_result(raw)
    if not result["valid"]:
        logger.warning("[change_chapter] Request consistency evaluator returned invalid JSON; keeping original request.")
        worker.step_started.emit("Change request consistency check could not be parsed; using the original request.")
        return request

    issues = result["issues"]
    if result["consistent"] or not issues:
        logger.info("[change_chapter] Change request consistency check: no internal problems detected.")
        worker.step_started.emit("Change request consistency check passed.")
        return request

    logger.info("[change_chapter] Change request consistency found %d issue(s); repairing request before planning.", len(issues))
    worker.step_started.emit(
        f"Change request has {len(issues)} consistency issue(s). Repairing request before planning..."
    )

    repair_system = prompts.render("change_chapter/consistency_repair_system")
    repair_user = prompts.render(
        "change_chapter/consistency_repair_user",
        change_request=request,
        issues=_issues_text(issues),
    )
    repaired = worker._run_lean_inference(
        TaskType.CHANGE_CHAPTER,
        repair_system,
        repair_user,
        max_tokens=min(worker._content_max_tokens(), 1024),
    )
    repaired = (repaired or "").strip()

    original_len = max(1, len(request.split()))
    repaired_len = len(repaired.split())
    if (
        not repaired
        or _looks_like_wrapped_output(repaired)
        or repaired_len < int(original_len * _MIN_REPAIR_RATIO)
    ):
        logger.warning(
            "[change_chapter] Repaired request looked unsafe (words %d -> %d); keeping original request.",
            original_len,
            repaired_len,
        )
        worker.step_started.emit("Request repair was unsafe or incomplete; using the original request.")
        return request

    logger.info(
        "[change_chapter] Change request repair accepted (words %d -> %d).",
        original_len,
        repaired_len,
    )
    worker.step_started.emit("Change request repaired; proceeding to Change Chapter planning.")
    return repaired


def install_change_run(module) -> None:
    """Wrap Change Chapter's entry point with a request-consistency precheck once."""
    if getattr(module, "_consistency_precheck_installed", False):
        return

    original_run = module.run

    def run_with_consistency(worker) -> None:
        request = worker.extra_input.strip()
        if not request:
            original_run(worker)
            return

        checked_request = evaluate_and_repair_request(worker, request)
        if checked_request and checked_request != request:
            worker.extra_input = checked_request

        original_run(worker)

    module.run = run_with_consistency
    module._consistency_precheck_installed = True
    logger.info("[change_chapter] Consistency precheck installed.")
