from __future__ import annotations

import json
import logging
from typing import Any, Optional

from engine import prompts
from engine.context import extract_outline_section, format_characters_block
from engine.models import Project, TaskType

logger = logging.getLogger("continuity")

# Approximate character budgets. validate() tightens these further from the
# active model context window so the validator stays usable on 4K models.
DEFAULT_MAX_CHAPTER_CHARS = 9000
DEFAULT_MAX_REFERENCE_CHARS = 2500
DEFAULT_MAX_PREVIOUS_CHARS = 3000


def _cap(text: str, limit: int, label: str) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return f"[... earlier {label} omitted for length ...]\n\n{text[-limit:]}"


def _cap_head_tail(text: str, limit: int, label: str) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = max(500, limit // 3)
    tail = max(500, limit - head - 80)
    return (
        f"[... middle of {label} omitted for length ...]\n\n"
        f"{text[:head].rstrip()}\n\n"
        f"[... middle omitted ...]\n\n"
        f"{text[-tail:].lstrip()}"
    )


def _chapter(project: Project, number: int):
    return next((c for c in project.chapters if c.number == number), None)


def _previous_chapters_reference(project: Project, chapter_number: int, limit: int) -> str:
    previous = [c for c in project.chapters if c.number < chapter_number and c.content.strip()]
    if not previous:
        return "(none — this is the first chapter)"

    # Prefer the immediately preceding chapter because it is the strongest
    # continuity reference; only add an older chapter when space remains.
    selected = previous[-2:]
    blocks: list[str] = []
    per_chapter = max(800, limit // len(selected))
    for chapter in selected:
        blocks.append(
            f"## Chapter {chapter.number}: {chapter.title}\n"
            f"{_cap_head_tail(chapter.content, per_chapter, f'Chapter {chapter.number}')}"
        )
    return _cap("\n\n".join(blocks), limit, "previous chapters")


def build_continuity_messages(
    project: Project,
    chapter_number: int,
    chapter_content: str,
    max_chapter_chars: int = DEFAULT_MAX_CHAPTER_CHARS,
    max_reference_chars: int = DEFAULT_MAX_REFERENCE_CHARS,
    max_previous_chars: int = DEFAULT_MAX_PREVIOUS_CHARS,
) -> list[dict[str, str]]:
    """Build a compact continuity-validation request."""
    outline_entry = _cap(
        extract_outline_section(project.outline, chapter_number) or "(none)",
        max_reference_chars,
        "outline entry",
    )
    characters = _cap(format_characters_block(project.characters) or "(none)", max_reference_chars, "character data")
    world = _cap(project.world, max_reference_chars, "world notes") or "(none)"
    memory = _cap(project.memory, max_reference_chars, "story memory") or "(none)"
    previous = _previous_chapters_reference(project, chapter_number, max_previous_chars)

    system = prompts.load_raw("continuity/system")
    user = prompts.render(
        "continuity/user",
        title=project.title,
        chapter_number=chapter_number,
        outline_entry=outline_entry,
        characters=characters,
        world=world,
        memory=memory,
        previous_chapters=previous,
        chapter_content=_cap_head_tail(chapter_content, max_chapter_chars, "chapter content"),
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_result(text: str) -> dict[str, Any]:
    """Parse and normalize the model's continuity JSON contract."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()

    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("No JSON object found")
        data = json.loads(raw[start:end + 1])
    except Exception as exc:
        logger.debug("Could not parse continuity result: %s", exc)
        return {"valid": False, "passed": False, "issues": [], "raw": raw}

    issues: list[dict[str, str]] = []
    raw_issues = data.get("issues", [])
    if isinstance(raw_issues, list):
        for item in raw_issues:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "error")).strip().lower()
            if severity not in {"error", "warning"}:
                severity = "error"
            description = str(item.get("description", "")).strip()
            if not description:
                continue
            issues.append({
                "category": str(item.get("category", "continuity")).strip() or "continuity",
                "severity": severity,
                "description": description,
                "chapter": str(item.get("chapter", "")).strip(),
            })

    has_errors = any(i["severity"] == "error" for i in issues)
    passed = bool(data.get("passed", False)) and not has_errors
    return {"valid": True, "passed": passed, "issues": issues, "raw": raw}


def validate(
    worker,
    chapter_number: int,
    chapter_content: Optional[str] = None,
    max_tokens: int = 700,
) -> dict[str, Any]:
    """Run continuity validation using the worker's existing lean inference path."""
    chapter = _chapter(worker.project, chapter_number)
    content = chapter_content if chapter_content is not None else (chapter.content if chapter else "")
    if not content.strip():
        return {
            "valid": True,
            "passed": False,
            "issues": [{
                "category": "continuity",
                "severity": "error",
                "description": "Chapter has no content to validate.",
                "chapter": str(chapter_number),
            }],
            "raw": "",
        }

    max_chapter_chars = DEFAULT_MAX_CHAPTER_CHARS
    max_reference_chars = DEFAULT_MAX_REFERENCE_CHARS
    max_previous_chars = DEFAULT_MAX_PREVIOUS_CHARS
    try:
        context_limit = int(worker._model_context_limit())
        # Reserve room for the evaluator response and its system instructions.
        prompt_char_budget = max(6000, (max(1024, context_limit - max_tokens - 500)) * 3)
        max_chapter_chars = min(DEFAULT_MAX_CHAPTER_CHARS, int(prompt_char_budget * 0.48))
        max_reference_chars = min(DEFAULT_MAX_REFERENCE_CHARS, int(prompt_char_budget * 0.10))
        max_previous_chars = min(DEFAULT_MAX_PREVIOUS_CHARS, int(prompt_char_budget * 0.12))
    except Exception:
        pass

    messages = build_continuity_messages(
        worker.project,
        chapter_number,
        content,
        max_chapter_chars=max_chapter_chars,
        max_reference_chars=max_reference_chars,
        max_previous_chars=max_previous_chars,
    )
    raw = worker._run_lean_inference(
        TaskType.REVIEW_CHAPTER,
        messages[0]["content"],
        messages[1]["content"],
        max_tokens=max_tokens,
    )
    result = parse_result(raw)
    if not result["valid"]:
        result["passed"] = False
    return result
