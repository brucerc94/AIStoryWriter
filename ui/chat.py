"""
Chat Panel.

Displays the full conversation history for a project.
Supports streaming token output, message bubbles, summarized message indicators.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from engine import storage
from engine.models import ChatMessage, MessageRole, Project, TaskType
from engine.workflow import WorkflowThread
from ui.styles import (
    COLOR_ACCENT,
    COLOR_ACCENT_DIM,
    COLOR_ASSISTANT_MSG,
    COLOR_BORDER,
    COLOR_BORDER_LIGHT,
    COLOR_ERROR,
    COLOR_SURFACE,
    COLOR_SURFACE_RAISED,
    COLOR_SUMMARY_MSG,
    COLOR_TEXT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    COLOR_USER_MSG,
    COLOR_WARNING,
    FONT_MONO,
    FONT_SANS,
    FONT_SERIF,
)

logger = logging.getLogger("ui.chat")


class AutoResizeTextEdit(QTextEdit):
    """
    A read-only QTextEdit that always sizes itself to fit its content —
    no scrollbars, no clipped text, no manual height bookkeeping.

    The key fix vs. a naive "measure document().size().height()" approach:
    the document's wrap width is explicitly pinned to the viewport's
    *actual* width before every measurement. Without this, a freshly
    created QTextEdit that hasn't been laid out yet reports a stale/zero
    width, so the wrapped height comes out wrong — text gets clipped
    inside a box that's too small, or the box visibly jitters as more
    text streams in and the (wrong) measurement keeps changing.
    """

    MIN_HEIGHT = 24
    MAX_HEIGHT = 8000  # generous ceiling so long chapters aren't clipped

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.document().setDocumentMargin(0)
        self.document().contentsChanged.connect(self._update_height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # The width changed (e.g. window resize, splitter drag) — re-wrap
        # and recompute height for the new width.
        self._update_height()

    def _update_height(self) -> None:
        width = self.viewport().width()
        if width <= 0:
            # Not laid out yet (e.g. just constructed, not yet added to a
            # visible layout). Retry once the event loop settles and Qt
            # has assigned a real geometry.
            QTimer.singleShot(0, self._update_height)
            return

        self.document().setTextWidth(width)
        doc_height = int(self.document().size().height()) + 8
        new_height = max(self.MIN_HEIGHT, min(doc_height, self.MAX_HEIGHT))
        if self.height() != new_height:
            self.setFixedHeight(new_height)
            # Make sure the change propagates up through parent layouts
            # (bubble -> messages area -> scroll area) immediately.
            self.updateGeometry()
            parent = self.parentWidget()
            if parent is not None:
                parent.updateGeometry()


class MessageBubble(QFrame):
    """A single chat message rendered as a styled frame."""

    def __init__(self, message: ChatMessage, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.message = message
        self._build(message)

    def _build(self, msg: ChatMessage) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(4)

        # Role label row
        role_row = QHBoxLayout()
        role_row.setContentsMargins(0, 0, 0, 0)

        if msg.role == MessageRole.USER:
            role_text = "You"
            bg = COLOR_USER_MSG
            role_color = COLOR_ACCENT
        elif msg.role == MessageRole.SUMMARY:
            role_text = "── Summary of earlier conversation ──"
            bg = COLOR_SUMMARY_MSG
            role_color = COLOR_TEXT_MUTED
        else:
            role_text = "Assistant"
            bg = COLOR_ASSISTANT_MSG
            role_color = COLOR_TEXT_DIM

        # Dim summarized messages slightly
        if msg.summarized:
            bg = COLOR_SURFACE
            role_text += " (summarized)"
            role_color = COLOR_TEXT_MUTED

        self.setStyleSheet(f"""
            QFrame {{
                background-color: {bg};
                border-radius: 8px;
                border: 1px solid {COLOR_BORDER};
            }}
        """)

        role_lbl = QLabel(role_text)
        role_lbl.setStyleSheet(
            f"color: {role_color}; font-size: 11px; font-weight: 600; "
            "text-transform: uppercase; letter-spacing: 0.05em; background: transparent; border: none;"
        )
        role_row.addWidget(role_lbl)
        role_row.addStretch()

        try:
            from datetime import datetime
            dt = datetime.fromisoformat(msg.timestamp)
            time_str = dt.strftime("%H:%M")
            time_lbl = QLabel(time_str)
            time_lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px; background: transparent; border: none;")
            role_row.addWidget(time_lbl)
        except Exception:
            pass

        layout.addLayout(role_row)

        # Content — auto-resizing, no scrollbars, never clips.
        content_edit = AutoResizeTextEdit()
        content_edit.setPlainText(msg.content)
        content_edit.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                border: none;
                color: {COLOR_TEXT};
                font-family: {FONT_SANS};
                font-size: 13px;
                padding: 0;
            }}
        """)
        layout.addWidget(content_edit)
        self._content_edit = content_edit
        # Force an initial measurement once this bubble has real geometry
        # (right after it's inserted into the messages layout).
        QTimer.singleShot(0, content_edit._update_height)

    def append_text(self, text: str) -> None:
        """Called during streaming to append tokens."""
        cursor = self._content_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        self._content_edit.setTextCursor(cursor)


class StreamingBubble(QFrame):
    """Temporary bubble shown while streaming. Replaced by MessageBubble when done."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(4)
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {COLOR_ASSISTANT_MSG};
                border-radius: 8px;
                border: 1px solid {COLOR_BORDER};
            }}
        """)

        role_lbl = QLabel("Assistant")
        role_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 11px; font-weight: 600; "
            "text-transform: uppercase; background: transparent; border: none;"
        )
        layout.addWidget(role_lbl)

        self.content_edit = AutoResizeTextEdit()
        self.content_edit.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                border: none;
                color: {COLOR_TEXT};
                font-family: {FONT_SANS};
                font-size: 13px;
                padding: 0;
            }}
        """)
        layout.addWidget(self.content_edit)
        QTimer.singleShot(0, self.content_edit._update_height)

    def append_token(self, token: str) -> None:
        cursor = self.content_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(token)
        self.content_edit.setTextCursor(cursor)
        # No manual height math needed here anymore — AutoResizeTextEdit
        # recomputes its own height via document().contentsChanged.

    def get_text(self) -> str:
        return self.content_edit.toPlainText()


class ChatMessagesArea(QWidget):
    """Scrollable area containing all message bubbles."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._layout.setSpacing(8)
        self._layout.addStretch()
        self._streaming_bubble: Optional[StreamingBubble] = None

    def load_messages(self, messages: list[ChatMessage]) -> None:
        """Render all messages. Called when opening a project."""
        # Clear existing
        while self._layout.count() > 1:  # keep the stretch
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                continue
            bubble = MessageBubble(msg)
            self._layout.insertWidget(self._layout.count() - 1, bubble)

    def add_message(self, message: ChatMessage) -> None:
        """Add a single message bubble."""
        if message.role == MessageRole.SYSTEM:
            return
        bubble = MessageBubble(message)
        self._layout.insertWidget(self._layout.count() - 1, bubble)

    def begin_streaming(self) -> None:
        """Show a streaming bubble for the incoming assistant response.

        Safe to call even if a previous streaming bubble was never
        finalized (e.g. a step like "Clearing chat..." that has no
        matching step_finished) — any leftover bubble is discarded first
        so we never leave an empty orphaned bubble sitting in the log.
        """
        if self._streaming_bubble is not None:
            stale = self._streaming_bubble
            self._streaming_bubble = None
            try:
                stale.setParent(None)
                stale.deleteLater()
            except RuntimeError:
                pass
        self._streaming_bubble = StreamingBubble()
        self._layout.insertWidget(self._layout.count() - 1, self._streaming_bubble)

    def append_streaming_token(self, token: str) -> None:
        if self._streaming_bubble:
            self._streaming_bubble.append_token(token)

    def finalize_streaming(self) -> Optional[str]:
        """Remove streaming bubble, return accumulated text."""
        if self._streaming_bubble:
            bubble = self._streaming_bubble
            self._streaming_bubble = None
            try:
                text = bubble.get_text()
            except RuntimeError:
                # The underlying Qt/C++ object was already destroyed — e.g.
                # a project reload rebuilt the message list out from under
                # an in-flight generation. Nothing to recover here, but we
                # must NOT let this exception escape: callers rely on this
                # method completing so they can still emit project_updated
                # and refresh the rest of the UI.
                logger.warning(
                    "finalize_streaming: bubble was already destroyed "
                    "(likely a project reload happened mid-generation)."
                )
                return None
            try:
                bubble.setParent(None)
                bubble.deleteLater()
            except RuntimeError:
                pass
            return text
        return None

    def add_error_notice(self, error: str) -> None:
        frame = QFrame()
        frame.setStyleSheet(f"background: #2d1515; border-radius: 6px; border: 1px solid {COLOR_ERROR};")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(12, 8, 12, 8)
        lbl = QLabel(f"Error: {error}")
        lbl.setStyleSheet(f"color: {COLOR_ERROR}; font-size: 12px; background: transparent; border: none;")
        lbl.setWordWrap(True)
        fl.addWidget(lbl)
        self._layout.insertWidget(self._layout.count() - 1, frame)


class ChatPanel(QWidget):
    """
    Full chat panel. Contains the message scroll area and input box.
    Manages the WorkflowThread for chat interactions.
    """

    # Emitted when the project's data changes (so other panels can refresh)
    project_updated = Signal()
    # Emitted when the workflow thread starts/stops, so other panels can disable their generate buttons
    # Args: (is_busy: bool, project_name: str)
    busy_changed = Signal(bool, str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._thread: Optional[WorkflowThread] = None
        self._current_task: Optional[TaskType] = None
        self._settings = None
        # Tracks which project the currently-running thread belongs to,
        # independently of self._project (what the user is *viewing* now).
        self._active_project: Optional[Project] = None
        self._build_ui()

    def set_settings(self, settings) -> None:
        self._settings = settings

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header toolbar
        header = QFrame()
        header.setFixedHeight(40)
        header.setStyleSheet(
            f"background: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_BORDER};"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 0, 10, 0)

        header_lbl = QLabel("Chat")
        header_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 12px; font-weight: 600; "
            "text-transform: uppercase; letter-spacing: 0.05em; background: transparent;"
        )
        header_layout.addWidget(header_lbl)
        header_layout.addStretch()

        self.clear_chat_btn = QPushButton("🗑 Clear Chat")
        self.clear_chat_btn.setObjectName("subtle")
        self.clear_chat_btn.setToolTip(
            "Clears the conversation history only. Synopsis, outline, "
            "chapters, world notes, and story memory are untouched."
        )
        self.clear_chat_btn.clicked.connect(self._clear_chat)
        header_layout.addWidget(self.clear_chat_btn)

        layout.addWidget(header)

        # Status bar for model loading / task progress — a small card that
        # reads as "the AI is actively working", not a frozen app.
        self.status_bar = QFrame()
        self.status_bar.setObjectName("aiStatusBar")
        self.status_bar.setFixedHeight(36)
        status_layout = QVBoxLayout(self.status_bar)
        status_layout.setContentsMargins(16, 4, 16, 4)
        status_layout.setSpacing(2)

        status_top = QHBoxLayout()
        status_top.setSpacing(8)
        self.status_icon = QLabel("⠋")
        self.status_icon.setObjectName("aiStatusIcon")
        self.status_icon.setFixedWidth(14)
        status_top.addWidget(self.status_icon)
        self.status_label = QLabel("")
        self.status_label.setObjectName("aiStatusLabel")
        status_top.addWidget(self.status_label, 1)
        status_layout.addLayout(status_top)

        self.status_progress = QProgressBar()
        self.status_progress.setObjectName("aiProgress")
        self.status_progress.setRange(0, 0)  # indeterminate — busy, not stalled
        self.status_progress.setTextVisible(False)
        self.status_progress.setFixedHeight(3)
        status_layout.addWidget(self.status_progress)

        self.status_bar.hide()
        layout.addWidget(self.status_bar)

        # Banner shown when the user switches to another project while a
        # thread is still running for the previous one.
        self._bg_banner = QFrame()
        self._bg_banner.setObjectName("bgBanner")
        self._bg_banner.setFixedHeight(30)
        self._bg_banner.setStyleSheet(
            f"QFrame#bgBanner {{ background: {COLOR_ACCENT_DIM}; border-bottom: 1px solid {COLOR_ACCENT}; }}"
        )
        bg_layout = QHBoxLayout(self._bg_banner)
        bg_layout.setContentsMargins(14, 0, 14, 0)
        self._bg_label = QLabel("")
        self._bg_label.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-size: 12px; background: transparent;"
        )
        bg_layout.addWidget(self._bg_label)
        self._bg_banner.hide()
        layout.addWidget(self._bg_banner)

        # Cycles the small spinner glyph while a task is running, so the
        # status bar visibly animates instead of just holding static text.
        self._spinner_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._spinner_index = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(90)
        self._spinner_timer.timeout.connect(self._advance_spinner)

        # Scroll area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; }")
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.messages_widget = ChatMessagesArea()
        self.scroll_area.setWidget(self.messages_widget)
        layout.addWidget(self.scroll_area, 1)

        # Input area
        input_frame = QFrame()
        input_frame.setStyleSheet(
            f"background: {COLOR_SURFACE}; border-top: 1px solid {COLOR_BORDER};"
        )
        input_frame.setFixedHeight(110)
        input_layout = QVBoxLayout(input_frame)
        input_layout.setContentsMargins(12, 10, 12, 10)
        input_layout.setSpacing(6)

        # Input row
        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        self.input_edit = QPlainTextEdit()
        self.input_edit.setPlaceholderText(
            "Ask about your story, request a rewrite, brainstorm ideas… (Enter to send, Shift+Enter for newline)"
        )
        self.input_edit.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {COLOR_SURFACE_RAISED};
                border: 1px solid {COLOR_BORDER_LIGHT};
                border-radius: 8px;
                padding: 8px 12px;
                font-size: 14px;
                color: {COLOR_TEXT};
            }}
            QPlainTextEdit:focus {{
                border-color: {COLOR_ACCENT};
            }}
        """)
        self.input_edit.setFixedHeight(70)
        self.input_edit.keyPressEvent = self._input_key_press
        input_row.addWidget(self.input_edit, 1)

        btn_col = QVBoxLayout()
        btn_col.setSpacing(4)

        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("accent")
        self.send_btn.setFixedWidth(82)
        self.send_btn.setMinimumHeight(34)
        self.send_btn.setStyleSheet("QPushButton#accent { font-size: 13px; font-weight: 600; }")
        self.send_btn.clicked.connect(self._send_message)
        btn_col.addWidget(self.send_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setFixedWidth(82)
        self.stop_btn.setMinimumHeight(34)
        self.stop_btn.hide()
        self.stop_btn.clicked.connect(self._stop_generation)
        btn_col.addWidget(self.stop_btn)

        input_row.addLayout(btn_col)
        input_layout.addLayout(input_row)
        layout.addWidget(input_frame)

        # Welcome state
        self._show_empty_state()

    def _show_empty_state(self) -> None:
        if not self._project:
            lbl = QLabel("Select or create a project to start writing.")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 14px; font-weight: 500; padding: 24px;")
            self.messages_widget._layout.insertWidget(
                self.messages_widget._layout.count() - 1, lbl
            )

    def load_project(self, project: Project) -> None:
        self._project = project
        if self._thread and self._thread.isRunning():
            # A thread is still running for _active_project (the old project).
            # Don't touch the message list (the streaming bubble lives there),
            # and don't reassign _active_project — the thread still owns that.
            # Show a banner so the user knows background work is continuing.
            logger.info(
                "load_project: thread running for '%s', switching view to '%s'.",
                self._active_project.title if self._active_project else "?",
                project.title,
            )
            self._update_bg_banner()
            self.messages_widget.load_messages(project.chat_messages)
            QTimer.singleShot(100, self._scroll_to_bottom)
            return
        self._active_project = project
        self.messages_widget.load_messages(project.chat_messages)
        QTimer.singleShot(100, self._scroll_to_bottom)

    def sync_project_reference(self, project: Project) -> None:
        """
        Swap which Project object we point to, without touching the message
        UI at all. Used right after a task finishes to pick up the freshly
        reloaded-from-disk object that Story/Models panels now use — so a
        later task_requested (e.g. "review chapter 2") builds its
        WorkflowThread from the same object those panels just mutated,
        instead of a stale copy that never saw the change. Safe to call
        even while a thread is technically still winding down, since it
        never rebuilds any widgets.
        """
        self._project = project
        # If no thread is running, keep _active_project in sync too.
        if not (self._thread and self._thread.isRunning()):
            self._active_project = project

    def _scroll_to_bottom(self) -> None:
        sb = self.scroll_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _input_key_press(self, event) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent
        if event.key() == Qt.Key_Return and not (event.modifiers() & Qt.ShiftModifier):
            self._send_message()
        else:
            QPlainTextEdit.keyPressEvent(self.input_edit, event)

    def _send_message(self) -> None:
        if not self._project:
            return
        text = self.input_edit.toPlainText().strip()
        if not text:
            return
        if self._thread and self._thread.isRunning():
            return

        self.input_edit.clear()

        # Add user message to UI immediately
        from engine.models import ChatMessage, MessageRole
        from datetime import datetime
        user_msg = ChatMessage(role=MessageRole.USER, content=text)
        self.messages_widget.add_message(user_msg)
        self.messages_widget.begin_streaming()
        self._scroll_to_bottom()

        self._active_project = self._project  # lock in the project for this run
        self._set_busy(True)
        self._thread = WorkflowThread(
            project=self._active_project,
            task=TaskType.CHAT,
            extra_input=text,
            settings=self._settings,
        )
        self._current_task = TaskType.CHAT
        self._thread.token_received.connect(self._on_token)
        self._thread.step_finished.connect(self._on_step_finished)
        self._thread.error_occurred.connect(self._on_error)
        self._thread.model_loading.connect(self._on_model_loading)
        self._thread.finished.connect(self._on_finished)
        self._thread.start()

    def run_task(self, task: TaskType, extra_input: str = "") -> None:
        """Called from the Story panel to run non-chat tasks."""
        if not self._project:
            return
        if self._thread and self._thread.isRunning():
            return

        self._active_project = self._project  # lock in the project for this run
        self.messages_widget.begin_streaming()
        self._scroll_to_bottom()
        self._set_busy(True)

        self._thread = WorkflowThread(
            project=self._active_project,
            task=task,
            extra_input=extra_input,
            settings=self._settings,
        )
        self._current_task = task
        self._thread.token_received.connect(self._on_token)
        self._thread.step_finished.connect(self._on_step_finished)
        self._thread.error_occurred.connect(self._on_error)
        self._thread.model_loading.connect(self._on_model_loading)
        self._thread.step_started.connect(self._on_step_started)
        self._thread.clear_chat_requested.connect(lambda: self.clear_chat(prompt=False))
        self._thread.finished.connect(self._on_finished)
        self._thread.start()

    def _on_token(self, token: str) -> None:
        self.messages_widget.append_streaming_token(token)
        self._scroll_to_bottom()

    def _on_step_started(self, desc: str) -> None:
        self._show_status(desc)
        # "Write Book" (and similar multi-step tasks) fire step_started once
        # per chapter/sub-step, but the streaming bubble from the PREVIOUS
        # step was already removed by finalize_streaming() in
        # _on_step_finished(). Without opening a fresh bubble here, tokens
        # for every step after the first one have nowhere to go — the
        # status label keeps updating, but the chat looks frozen even
        # though the model is actively generating. Re-open a bubble for
        # each new step so streaming stays visible the whole way through.
        self.messages_widget.begin_streaming()
        self._scroll_to_bottom()

    def _on_model_loading(self, msg: str) -> None:
        self._show_status(msg)

    def _on_step_finished(self, step: str, result: str) -> None:
        self.messages_widget.finalize_streaming()
        # The worker already added the messages to _active_project.chat_messages.
        # Always reload from _active_project (the project the thread belongs to),
        # NOT self._project (the one the user might currently be viewing).
        if self._active_project:
            # Only update the visible message list if the user is still
            # looking at the same project the thread is working on.
            if self._project and self._project.id == self._active_project.id:
                self.messages_widget.load_messages(self._active_project.chat_messages)
        self._scroll_to_bottom()
        self.project_updated.emit()

    def _on_error(self, error: str) -> None:
        self.messages_widget.finalize_streaming()
        self.messages_widget.add_error_notice(error)
        self._scroll_to_bottom()

    def _on_finished(self) -> None:
        self._set_busy(False)
        self._hide_status()
        self._bg_banner.hide()
        self._scroll_to_bottom()
        self._current_task = None
        # After the thread finishes, _active_project and _project should
        # agree on which project the user is viewing.
        if self._project and self._active_project and self._project.id != self._active_project.id:
            # User was viewing a different project while the thread ran.
            # Now that the thread is done, keep _active_project pointed at
            # whatever the user is currently viewing.
            self._active_project = self._project
        self.busy_changed.emit(False, "")

    def _stop_generation(self) -> None:
        if self._thread:
            if self._current_task == TaskType.WRITE_BOOK:
                self._thread.request_stop_after_current_chapter()
                self.stop_btn.setEnabled(False)
                self.stop_btn.setText("Stopping…")
                self._show_status("Stopping — will finish the current chapter...")
            else:
                self._thread.cancel()
                self.stop_btn.setEnabled(False)
                self.stop_btn.setText("Stopping…")
                self._show_status("Stopping — finishing the current token…")

    def _clear_chat(self) -> None:
        self.clear_chat()

    def clear_chat(self, prompt: bool = True) -> None:
        """
        Clears the conversation history only. Synopsis, outline, chapters,
        world notes, and story memory all live on the Project separately
        and are completely untouched — this only resets project.chat_messages
        and the rolling conversation summary, so you can start a fresh
        conversation without losing any of the actual story.
        """
        if not self._project:
            return
        if prompt and self._thread and self._thread.isRunning():
            QMessageBox.information(
                self, "Busy", "Wait for the current task to finish before clearing the chat."
            )
            return

        if prompt:
            reply = QMessageBox.question(
                self,
                "Clear Chat",
                "Clear the conversation history?\n\n"
                "This only resets the chat — your synopsis, outline, chapters, "
                "world notes, and story memory are kept exactly as they are.\n\n"
                "This cannot be undone.",
                QMessageBox.Yes | QMessageBox.Cancel,
            )
            if reply != QMessageBox.Yes:
                return

        self._project.chat_messages = []
        storage.save_project(self._project)

        self.messages_widget.load_messages(self._project.chat_messages)
        self.project_updated.emit()

    def _set_busy(self, busy: bool) -> None:
        self.send_btn.setVisible(not busy)
        self.stop_btn.setVisible(busy)
        self.stop_btn.setEnabled(True)
        self.stop_btn.setText("Stop")
        self.input_edit.setEnabled(not busy)
        project_name = self._active_project.title if self._active_project else ""
        self.busy_changed.emit(busy, project_name)

    def _update_bg_banner(self) -> None:
        """Show/hide the background-generation banner based on thread state."""
        if self._thread and self._thread.isRunning() and self._active_project:
            viewing_different = (
                self._project is None
                or self._project.id != self._active_project.id
            )
            if viewing_different:
                self._bg_label.setText(
                    f"⟳ Generating in background: \"{self._active_project.title}\"…"
                )
                self._bg_banner.show()
                return
        self._bg_banner.hide()

    def _show_status(self, msg: str) -> None:
        self.status_label.setText(msg)
        self.status_icon.setText(self._spinner_frames[self._spinner_index])
        self.status_bar.show()
        if not self._spinner_timer.isActive():
            self._spinner_timer.start()

    def _hide_status(self) -> None:
        self._spinner_timer.stop()
        self.status_bar.hide()
        self.status_label.setText("")

    def _advance_spinner(self) -> None:
        self._spinner_index = (self._spinner_index + 1) % len(self._spinner_frames)
        self.status_icon.setText(self._spinner_frames[self._spinner_index])
