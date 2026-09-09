"""
Prompt loader.

Every prompt/instruction text the app sends to a model lives as a plain
text file under engine/prompts/, organized by workflow
(e.g. engine/prompts/change_chapter/full_rewrite_system.txt). Python code
never embeds prompt copy inline — it only decides *which* template to load
and *which* data to fill it with; the template decides how that data reads
to the model.

Template syntax is intentionally minimal: `{{variable_name}}` placeholders,
substituted via a controlled regex rather than str.format()/string.Template
so that literal braces or `$` in story content (JSON examples, dialogue,
etc.) inside a template are never mistaken for template syntax.

Usage:
    from engine import prompts
    text = prompts.render("change_chapter/full_rewrite_system", title=project.title)

Files are cached in memory after first read (they don't change at
runtime), but caching can be bypassed with `reload=True` — handy if a
template is edited while the app is running.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).parent / "prompts"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_CHECKLIST_ITEM_RE = re.compile(r"^\s*(\d+)\s*[.)-]\s+(.+?)\s*$", re.MULTILINE)

# These templates feed prose generation. Keep checklist IDs internal to
# planners/evaluators; the writer needs the requirement text and order, not
# the numeric labels that the model may accidentally reproduce in the story.
_WRITER_CHECKLIST_TEMPLATES = {
    "change_chapter/full_rewrite_user",
    "change_chapter/continue_user",
    "write_chapter/continuation_user",
}

_cache: dict[str, str] = {}
_last_writer_checklist_items: list[tuple[int, str]] = []


def _path_for(name: str) -> Path:
    return PROMPTS_DIR / f"{name}.txt"


def load_raw(name: str, reload: bool = False) -> str:
    """Return the raw (unsubstituted) contents of a prompt template."""
    if not reload and name in _cache:
        return _cache[name]
    path = _path_for(name)
    if not path.is_file():
        raise FileNotFoundError(
            f"Prompt template '{name}' not found at {path}. "
            "Prompt text belongs in engine/prompts/, not inline in Python."
        )
    text = path.read_text(encoding="utf-8")

    if text.endswith("\n"):
        text = text[:-1]
    _cache[name] = text
    return text


def _remember_writer_checklist(value: Any) -> None:
    """Remember the original numbered checklist for later continuation passes."""
    global _last_writer_checklist_items
    text = str(value or "").strip()
    if not text:
        return
    matches = list(_CHECKLIST_ITEM_RE.finditer(text))
    if not matches:
        return
    _last_writer_checklist_items = [
        (int(match.group(1)), match.group(2).strip())
        for match in matches
        if match.group(2).strip()
    ]


def _last_completed_beat(completed_ids: Any) -> str:
    """Return the latest checklist beat already confirmed complete."""
    try:
        done_ids = {
            int(part.strip())
            for part in str(completed_ids or "").split(",")
            if part.strip()
        }
    except (TypeError, ValueError):
        done_ids = set()

    if not done_ids or not _last_writer_checklist_items:
        return "(none — use the exact current chapter ending as the starting point)"

    for item_id, text in reversed(_last_writer_checklist_items):
        if item_id in done_ids:
            return text
    return "(none — use the exact current chapter ending as the starting point)"


def _format_writer_checklist(value: Any) -> str:
    """Return checklist requirements in writer-safe bullet form, without IDs."""
    text = str(value or "").strip()
    if not text:
        return text

    matches = list(_CHECKLIST_ITEM_RE.finditer(text))
    if not matches:
        return text

    items = [m.group(2).strip() for m in matches if m.group(2).strip()]
    return "\n".join(f"- {item}" for item in items) or text


def render(name: str, **variables: Any) -> str:
    """
    Load the named template and substitute every {{placeholder}} with the
    matching keyword argument. Raises KeyError with the template name and
    missing variable if the caller forgot to supply something the template
    needs — fails loudly instead of silently sending "{{foo}}" to a model.

    Prose-generation templates receive a writer-safe checklist view: item
    numbers are stripped while preserving the requirement text and order.
    Planner/evaluator templates retain the original numbered checklist.
    """
    text = load_raw(name)
    render_vars = dict(variables)

    # Capture the original checklist only when a prose-generation call sees
    # the full checklist. Continuation subsets never overwrite this state.
    if name in _WRITER_CHECKLIST_TEMPLATES and "checklist" in render_vars:
        _remember_writer_checklist(render_vars["checklist"])

    if name in _WRITER_CHECKLIST_TEMPLATES:
        for key in ("checklist", "remaining_checklist"):
            if key in render_vars:
                render_vars[key] = _format_writer_checklist(render_vars[key])
        if "completed_ids" in render_vars:
            render_vars["last_completed_beat"] = _last_completed_beat(render_vars["completed_ids"])

    # Write Chapter's initial checklist is embedded through a generic section
    # template. Only checklist/requirements sections are transformed so
    # canon, style, intent, and other section bodies remain untouched.
    if name == "change_chapter/section":
        heading = str(render_vars.get("heading", "")).lower()
        if "checklist" in heading or "story requirements" in heading:
            if "body" in render_vars:
                _remember_writer_checklist(render_vars["body"])
                render_vars["body"] = _format_writer_checklist(render_vars["body"])

    def _substitute(match: re.Match) -> str:
        key = match.group(1)
        if key not in render_vars:
            raise KeyError(
                f"Prompt template '{name}' requires variable "
                f"'{{{{{key}}}}}' but it wasn't provided."
            )
        return str(render_vars[key])

    return _PLACEHOLDER_RE.sub(_substitute, text)


def clear_cache() -> None:
    global _last_writer_checklist_items
    _cache.clear()
    _last_writer_checklist_items = []
