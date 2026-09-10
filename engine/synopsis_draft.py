"""Synopsis / Draft normalization pipeline.

The Synopsis tab is the author's raw story draft. Before characters and world
are extracted, the draft is checked for internal consistency, repaired when
necessary, then rewritten into a richer planning-oriented draft using the
full Author Profile. Each model call uses a fresh inference context.
"""

from __future__ import annotations

import json
import logging

from engine import prompts, storage
from engine.models import TaskType

logger = logging.getLogger("workflow")

_CONSISTENCY_EVAL_TOKENS = 384
_CONSISTENCY_REPAIR_TOKENS = 1024
_MIN_REPAIR_RATIO = 0.60
_MAX_CONSISTENCY_CHECKS = 2
_MIN_DRAFT_CHARS = 600


def _parse_consistency_result(raw: str) -> dict:
    text = (raw or "").strip()
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object")
        data = json.loads(text[start : end + 1])
        if not isinstance(data, dict):
            raise ValueError("top-level JSON is not an object")
        issues = data.get("issues", [])
        if not isinstance(issues, list):
            issues = []
        return {
            "valid": True,
            "consistent": bool(data.get("consistent", False)),
            "issues": issues,
            "raw": text,
        }
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {
            "valid": False,
            "consistent": False,
            "issues": [],
            "raw": text,
            "error": str(exc),
        }


def _issues_text(issues: list[object]) -> str:
    lines: list[str] = []
    for issue in issues[:12]:
        if isinstance(issue, dict):
            issue_type = str(issue.get("type", "continuity")).strip()
            description = str(issue.get("description", issue.get("issue", ""))).strip()
            if description:
                lines.append(f"- {issue_type}: {description}")
        else:
            value = str(issue).strip()
            if value:
                lines.append(f"- {value}")
    return "\n".join(lines) or "- Clear internal contradiction or sequencing problem detected."


def _build_author_profile(worker) -> str:
    intent = worker.project.author_intent.to_prompt_fragment().strip()
    style = worker.project.writing_style.to_prompt_fragment().strip()
    parts: list[str] = []
    if intent:
        parts.append(f"CREATIVE INTENT:\n{intent}")
    if style:
        parts.append(f"WRITING STYLE:\n{style}")
    return "\n\n".join(parts).strip() or "(none specified)"


def _check_consistency(worker, draft: str) -> dict:
    system = prompts.load_raw("synopsis_draft/consistency_system")
    user = prompts.render("synopsis_draft/consistency_user", draft=draft)
    for attempt in range(1, _MAX_CONSISTENCY_CHECKS + 1):
        logger.info("[synopsis_draft] Consistency check %d/%d (fresh context).", attempt, _MAX_CONSISTENCY_CHECKS)
        raw = worker._run_lean_inference(
            TaskType.REVIEW_CHAPTER,
            system,
            user,
            max_tokens=_CONSISTENCY_EVAL_TOKENS,
        )
        result = _parse_consistency_result(raw)
        if result["valid"]:
            return result
        logger.warning("[synopsis_draft] Consistency check parse failed: %s", result.get("error", "invalid result"))
    return {"valid": False, "consistent": True, "issues": [], "raw": ""}


def _repair_draft(worker, draft: str, issues: list[object]) -> str:
    worker.step_started.emit("Repairing draft consistency...")
    system = prompts.load_raw("synopsis_draft/repair_system")
    user = prompts.render(
        "synopsis_draft/repair_user",
        draft=draft,
        issues=_issues_text(issues),
    )
    logger.info("[synopsis_draft] Repairing draft in a fresh context.")
    repaired = worker._run_lean_inference(
        TaskType.WRITE_SYNOPSIS,
        system,
        user,
        max_tokens=_CONSISTENCY_REPAIR_TOKENS,
    ).strip()
    original_words = max(1, len(draft.split()))
    repaired_words = len(repaired.split())
    if not repaired or repaired_words < int(original_words * _MIN_REPAIR_RATIO):
        logger.warning(
            "[synopsis_draft] Unsafe repair rejected (words %d -> %d); retaining original draft.",
            original_words,
            repaired_words,
        )
        return draft
    return repaired


def _generate_draft(worker, source: str) -> str:
    system = prompts.load_raw("synopsis_draft/draft_system")
    user = prompts.render(
        "synopsis_draft/draft_user",
        source=source,
        author_profile=_build_author_profile(worker),
        title=worker.project.title,
    )
    worker.step_started.emit("Developing Synopsis / Draft with Author Profile...")
    logger.info("[synopsis_draft] Draft generation in a fresh context.")
    draft = worker._run_lean_inference(
        TaskType.WRITE_SYNOPSIS,
        system,
        user,
        max_tokens=worker._content_max_tokens(),
    ).strip()
    return draft


def run_write_synopsis(worker) -> None:
    source = (worker.project.synopsis or "").strip()
    if not source:
        worker.error_occurred.emit(
            "Synopsis / Draft is empty. Write your story first, then generate the draft."
        )
        return

    worker.step_started.emit("Checking Synopsis / Draft consistency...")
    checked_source = source
    result = _check_consistency(worker, checked_source)

    if result["valid"] and not result["consistent"] and result["issues"]:
        checked_source = _repair_draft(worker, checked_source, result["issues"])

        # Fresh context: validate the repaired draft independently.
        worker.step_started.emit("Rechecking repaired Synopsis / Draft consistency...")
        result = _check_consistency(worker, checked_source)
        if result["valid"] and not result["consistent"] and result["issues"]:
            worker.error_occurred.emit(
                "Synopsis / Draft still contains consistency problems after repair. "
                "No draft or canon was changed."
            )
            return

    # Fresh context: the final draft writer sees only the checked source + Author Profile.
    final_draft = _generate_draft(worker, checked_source)
    if len(final_draft) < _MIN_DRAFT_CHARS:
        worker.error_occurred.emit(
            "Could not generate a sufficiently developed Synopsis / Draft. Nothing was saved."
        )
        return

    worker.project.synopsis = final_draft

    # Canon is created only AFTER the final normalized draft exists.
    worker.step_started.emit("Building Characters from final draft...")
    worker._extract_and_merge_characters(final_draft)
    worker.step_started.emit("Building World from final draft...")
    worker._update_world_incremental(final_draft, source_type="synopsis_draft")

    storage.save_project(worker.project)
    worker.step_finished.emit("Synopsis / Draft", final_draft)
    logger.info(
        "[synopsis_draft] Final draft saved; Characters and World updated from final draft."
    )


def install(module) -> None:
    if getattr(module, "_synopsis_draft_installed", False):
        return
    worker_cls = getattr(module, "WorkflowWorker", None)
    if worker_cls is None:
        raise RuntimeError("Synopsis draft hook could not find WorkflowWorker.")
    worker_cls._run_write_synopsis = run_write_synopsis
    worker_cls._synopsis_draft_installed = True
    module._synopsis_draft_installed = True
    logger.info("[synopsis_draft] Synopsis / Draft pipeline installed.")
