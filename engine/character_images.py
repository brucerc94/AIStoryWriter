"""
Persisting the result of a character portrait generation.

The generation runs in a background thread and may finish after the user has
switched projects. So the result is always written to the project it was
started for, identified by its id — never to whichever project is on screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from engine import storage
from engine.models import Project


@dataclass(frozen=True)
class PortraitOutcome:
    project_id: str
    character_id: str
    ok: bool
    image_ref: Optional[dict] = None
    error: str = ""


def mark_generating(project: Project, character_id: str) -> None:
    char = _find(project, character_id)
    if char is None:
        return
    char.image_status = "Generating"
    char.image_error = ""
    storage.save_project(project)


def record_outcome(outcome: PortraitOutcome, loaded: Optional[Project] = None) -> Optional[Project]:
    """
    Store the portrait result on the right project.

    `loaded` is the in-memory project the UI currently holds. It is reused
    when it is the same project (so the UI stays in sync); otherwise the
    project is read from storage. Returns the project that was saved, or None
    if it or the character no longer exists.
    """
    if loaded is not None and loaded.id == outcome.project_id:
        project = loaded
    else:
        project = storage.load_project(outcome.project_id)
    if project is None:
        return None

    char = _find(project, outcome.character_id)
    if char is None:
        return None

    if outcome.ok and outcome.image_ref:
        char.image_ref = outcome.image_ref
        char.image_status = "Ready"
        char.image_error = ""
    else:
        char.image_status = "Error"
        char.image_error = outcome.error or "Image generation failed."

    storage.save_project(project)
    return project


def _find(project: Project, character_id: str):
    return next((c for c in project.characters if c.id == character_id), None)
