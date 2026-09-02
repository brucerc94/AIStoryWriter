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

_cache: dict[str, str] = {}


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


def render(name: str, **variables: Any) -> str:
    """
    Load the named template and substitute every {{placeholder}} with the
    matching keyword argument. Raises KeyError with the template name and
    missing variable if the caller forgot to supply something the template
    needs — fails loudly instead of silently sending "{{foo}}" to a model.
    """
    text = load_raw(name)

    def _substitute(match: re.Match) -> str:
        key = match.group(1)
        if key not in variables:
            raise KeyError(
                f"Prompt template '{name}' requires variable "
                f"'{{{{{key}}}}}' but it wasn't provided."
            )
        return str(variables[key])

    return _PLACEHOLDER_RE.sub(_substitute, text)


def clear_cache() -> None:
    _cache.clear()
