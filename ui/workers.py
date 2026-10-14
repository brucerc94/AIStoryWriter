"""
Background workers shared by several panels.

Qt rule used everywhere here: a QObject worker is moved to its own QThread,
its run() is started by the thread, and it reports back with a signal. The
owner keeps references to both thread and worker until the job finishes.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

from engine.character_images import PortraitOutcome
from engine.image_workflow import generate_character_image


class CharacterPortraitWorker(QObject):
    finished = Signal(object)  # PortraitOutcome

    def __init__(self, project_id: str, character, settings) -> None:
        super().__init__()
        self.project_id = project_id
        self.character_id = character.id
        self._character = character
        self._settings = settings

    def run(self) -> None:
        try:
            ok, image_ref, error = generate_character_image(
                self.project_id, self._character, self._settings
            )
        except Exception as exc:
            ok, image_ref, error = False, None, str(exc)
        self.finished.emit(PortraitOutcome(
            project_id=self.project_id,
            character_id=self.character_id,
            ok=ok,
            image_ref=image_ref,
            error=error or "",
        ))


class CallWorker(QObject):
    """Runs one callable off the UI thread. Emits (True, value) or (False, error text)."""

    finished = Signal(object)

    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            outcome = (True, self._fn())
        except Exception as exc:
            outcome = (False, str(exc))
        self.finished.emit(outcome)


def start_worker(
    parent: QObject,
    worker: QObject,
    on_finished: Callable,
) -> QThread:
    """Move worker to a new thread, connect its finished signal and start it."""
    thread = QThread(parent)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(on_finished)
    worker.finished.connect(thread.quit)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread


def is_running(thread: Optional[QThread]) -> bool:
    return thread is not None and thread.isRunning()
