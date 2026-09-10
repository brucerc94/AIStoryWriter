"""Small UI bridge for the Synopsis / Draft workflow.

Kept separate from the main UI module so the existing Story panel layout and
public model field names remain unchanged.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("workflow")


def _patch_tab(tab) -> None:
    if getattr(tab, "_synopsis_draft_ui_patched", False):
        return

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel

        header = getattr(tab, "_gen_header", None)
        button = getattr(header, "action_btn", None) if header is not None else None
        editor = getattr(tab, "editor", None)
        if button is None or editor is None:
            return

        # Section title.
        labels = header.findChildren(QLabel, "", Qt.FindDirectChildrenOnly)
        if labels:
            labels[0].setText("Synopsis / Draft")

        # Button now sends the text currently in the editor, even if the user
        # has not pressed Save yet.
        try:
            button.clicked.disconnect()
        except (TypeError, RuntimeError):
            pass
        button.setText("✨ Generate Draft")

        def emit_current_draft() -> None:
            text = editor.get_text().strip()
            tab.task_requested.emit(__import__("engine.models", fromlist=["TaskType"]).TaskType.WRITE_SYNOPSIS, text)

        button.clicked.connect(emit_current_draft)

        # Empty-state Generate button uses the same current-editor source.
        generate_signal = getattr(editor, "generate_requested", None)
        if generate_signal is not None:
            try:
                generate_signal.disconnect()
            except (TypeError, RuntimeError):
                pass
            generate_signal.connect(emit_current_draft)

        # Make the editor wording reflect its role as the raw story draft.
        text_edit = getattr(editor, "editor", None)
        if text_edit is not None:
            text_edit.setPlaceholderText(
                "Write your story draft, synopsis, or raw story material here.\n\n"
                "Generate Draft will check consistency, repair contradictions if needed, "
                "develop the story using your Author Profile, and then build Characters and World."
            )

        # Rename the visible Story sub-tab while keeping Project.synopsis and
        # all persisted/internal names unchanged.
        story_panel = tab.parentWidget()
        tabs = getattr(story_panel, "tabs", None)
        if tabs is not None:
            index = tabs.indexOf(tab)
            if index >= 0:
                tabs.setTabText(index, "Synopsis / Draft")

        tab._synopsis_draft_ui_patched = True
        logger.info("[synopsis_draft] Synopsis UI patched: current editor text is submitted directly.")
    except Exception:
        logger.exception("[synopsis_draft] Could not patch Synopsis UI.")


def patch_loaded_story_ui() -> None:
    try:
        from PySide6.QtWidgets import QApplication
        from ui.story import SynopsisTab
    except Exception:
        return

    app = QApplication.instance()
    if app is None:
        return

    for widget in app.allWidgets():
        if isinstance(widget, SynopsisTab):
            _patch_tab(widget)
