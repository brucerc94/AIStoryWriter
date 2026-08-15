"""
Targeted outline regeneration.

Adds a small, isolated workflow/UI extension without changing the existing
Generate Outline and Review Outline behavior.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import QInputDialog, QMessageBox, QPushButton

from engine import storage
from engine.models import ChatMessage, MessageRole, TaskType


SUGGESTION_MARKER = "__AI_STORY_WRITER_OUTLINE_SUGGESTION__"


def _regenerate_outline(self) -> None:
    """Apply one author suggestion to the current outline."""
    suggestion = self.extra_input.split(SUGGESTION_MARKER, 1)[1].strip()
    if not suggestion:
        self.error_occurred.emit("No outline suggestion was provided.")
        return

    current_outline = self.project.outline.strip()
    if not current_outline:
        self.error_occurred.emit("There is no outline to regenerate. Generate an outline first.")
        return

    self.step_started.emit("Regenerating outline from your suggestion...")

    prompt = (
        f"Modify the existing chapter-by-chapter outline for '{self.project.title}' "
        "according to the author's suggestion below.\n\n"
        "IMPORTANT RULES:\n"
        "1. The existing outline is the source of truth. Do not regenerate it from scratch.\n"
        "2. Apply the author's suggestion precisely and intelligently.\n"
        "3. Preserve every part that does not need to change.\n"
        "4. If the requested change creates consequences, update only the affected "
        "chapters and the minimum necessary downstream chapters for continuity.\n"
        "5. Preserve the existing chapter count unless the author explicitly asks to change it.\n"
        "6. Preserve chapter numbering, established character names, world rules, and continuity.\n"
        "7. Do not introduce unrelated plot changes, new arcs, or stylistic rewrites.\n"
        "8. Return the COMPLETE updated outline, not a diff, explanation, or commentary.\n"
        "9. Keep the same Markdown chapter structure used by the existing outline.\n\n"
        "AUTHOR'S SUGGESTION:\n"
        f"{suggestion}\n\n"
        "EXISTING OUTLINE:\n"
        "-----\n"
        f"{current_outline}\n"
        "-----\n\n"
        "Return only the complete updated outline."
    )

    result = self._run_inference(
        TaskType.GENERATE_OUTLINE,
        prompt,
        add_to_chat=False,
        max_tokens=self._content_max_tokens(),
    )
    if not result:
        return

    updated = result.strip()
    self.project.outline = updated
    self.project.chat_messages.append(
        ChatMessage(
            role=MessageRole.USER,
            content=f"Regenerate outline with suggestion: {suggestion}",
        )
    )
    self.project.chat_messages.append(
        ChatMessage(role=MessageRole.ASSISTANT, content=updated)
    )

    self._extract_and_merge_characters(updated)
    self._update_world_incremental(updated, source_type="outline")
    storage.save_project(self.project)
    self.step_finished.emit("Outline Regenerated", updated)


def _dispatch_wrapper(original_dispatch: Callable):
    def dispatch(self) -> None:
        if (
            self.task == TaskType.GENERATE_OUTLINE
            and self.extra_input.startswith(SUGGESTION_MARKER)
        ):
            _regenerate_outline(self)
            return
        original_dispatch(self)
    return dispatch


def _install_workflow_patch() -> None:
    from engine.workflow import WorkflowWorker

    if getattr(WorkflowWorker, "_outline_suggestion_patch", False):
        return
    original = WorkflowWorker._dispatch
    WorkflowWorker._dispatch = _dispatch_wrapper(original)
    WorkflowWorker._outline_suggestion_patch = True


def _install_outline_ui_patch() -> None:
    from ui.story import OutlineTab

    if getattr(OutlineTab, "_outline_suggestion_patch", False):
        return

    original_init = OutlineTab.__init__
    original_set_busy = OutlineTab.set_busy

    def patched_init(self, parent=None):
        original_init(self, parent)

        # OutlineTab builds the header as a QHBoxLayout directly inside the
        # root QVBoxLayout. itemAt(0) is therefore a layout, not a QWidget.
        header = self.layout().itemAt(0).layout()
        if header is None:
            return

        self._suggestion_btn = QPushButton("✨ Regenerate with suggestion")
        self._suggestion_btn.setObjectName("subtle")
        self._suggestion_btn.setToolTip(
            "Describe a change and regenerate the existing outline around that suggestion."
        )
        self._suggestion_btn.clicked.connect(lambda: _request_suggestion(self))
        header.insertWidget(header.count(), self._suggestion_btn)

    def patched_set_busy(self, busy: bool, project_name: str = ""):
        original_set_busy(self, busy, project_name)
        if hasattr(self, "_suggestion_btn"):
            self._suggestion_btn.setEnabled(not busy)
            if busy and project_name:
                self._suggestion_btn.setToolTip(f"Generating content for \"{project_name}\"…")
            else:
                self._suggestion_btn.setToolTip(
                    "Describe a change and regenerate the existing outline around that suggestion."
                )

    def _request_suggestion(tab) -> None:
        if not tab._project or not tab.editor.get_text().strip():
            QMessageBox.information(
                tab,
                "No outline yet",
                "Generate or write an outline first.",
            )
            return

        # Persist unsaved editor text before the workflow starts.
        tab.content_changed.emit(tab.editor.get_text())

        suggestion, accepted = QInputDialog.getMultiLineText(
            tab,
            "Regenerate Outline with Suggestion",
            "What would you like to change in the existing outline?",
            "",
        )
        if not accepted or not suggestion.strip():
            return

        tab.task_requested.emit(
            TaskType.GENERATE_OUTLINE,
            f"{SUGGESTION_MARKER}\n{suggestion.strip()}",
        )

    OutlineTab.__init__ = patched_init
    OutlineTab.set_busy = patched_set_busy
    OutlineTab._outline_suggestion_patch = True


def install() -> None:
    _install_workflow_patch()
    _install_outline_ui_patch()
