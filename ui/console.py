"""
Console / Logs panel.

Mirrors everything the terminal shows (INFO+ from the root logger, plus
engine.chat's "llm_engine" logger which disables propagation and would
otherwise never reach here) into a tab in the app itself — useful when
running as a packaged/windowed app where there's no visible terminal, or
just to avoid alt-tabbing out to check progress on a long "Write Book" run.
"""

from __future__ import annotations

import logging
import re

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.styles import (
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_SURFACE,
    COLOR_SURFACE_RAISED,
    COLOR_TEXT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    COLOR_WARNING,
)



_TOK_RE = re.compile(
    r"Generacion (?P<status>\w+): (?P<tokens>\d+) tokens en (?P<elapsed>[\d.]+)s"
    r" — (?P<toks>[\d.]+) tok/s \(TTFT (?P<ttft>[\d.]+) ms\)"
)



_EVAL_START_RE = re.compile(
    r"\[eval\] ── Chapter (?P<ch>\d+) completion check \(after pass (?P<pass>\d+)\)"
    r" ──\s+(?P<words>\d+) words"
)

_EVAL_ATTEMPT_RE = re.compile(r"\[eval\] Attempt (?P<n>\d+)/(?P<total>\d+)")

_EVAL_VERDICT_RE = re.compile(
    r"\[eval\] Attempt \d+: model said (?P<raw>[^\s→]+).*?verdict: (?P<verdict>TRUE|FALSE)"
)

_EVAL_UNPARSEABLE_RE = re.compile(r"\[eval\] Attempt \d+:.*unparseable.*: (?P<raw>.+)")

_EVAL_ALLFAILED_RE = re.compile(r"\[eval\] All \d+ attempts failed")

_EVAL_RESULT_RE = re.compile(r"\[eval\] ── Result for Chapter (?P<ch>\d+): (?P<action>.+?) ──")




MAX_LINES = 4000
TRIM_TO = 3000


class _QtLogSignal(QObject):
    """Plain QObject carrying the cross-thread signal — logging.Handler
    itself can't inherit QObject cleanly alongside its own __init__ chain
    in every PySide/Python combination, so the signal lives on a small
    companion object instead."""
    new_line = Signal(str)


class QtLogHandler(logging.Handler):
    """
    A logging.Handler that emits each formatted record as a Qt signal
    instead of writing to a stream. Safe to attach from a background
    thread (WorkflowThread) — Qt automatically queues the signal delivery
    onto the receiving widget's thread (the GUI thread) since the default
    connection type is Qt.AutoConnection and emitter/receiver live in
    different threads.
    """

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.bridge = _QtLogSignal()
        self.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        self.bridge.new_line.emit(msg)


class ConsolePanel(QWidget):
    """
    Read-only live view of the app's log output. Attaches a QtLogHandler
    to the root logger (catches everything logging.basicConfig's
    StreamHandler shows) AND to "llm_engine" specifically (engine.chat
    sets propagate=False on it, so it would otherwise never reach a
    handler on root).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._paused = False
        self._pending_while_paused: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)





        stats_bar = QFrame()
        stats_bar.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE_RAISED}; "
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 0; }}"
        )
        stats_layout = QHBoxLayout(stats_bar)
        stats_layout.setContentsMargins(12, 6, 12, 6)
        stats_layout.setSpacing(20)

        def _stat_pair(label_text: str) -> tuple[QLabel, QLabel]:
            lbl = QLabel(label_text)
            lbl.setStyleSheet(
                f"color: {COLOR_TEXT_MUTED}; font-size: 10px; font-weight: 600; "
                "text-transform: uppercase; letter-spacing: 0.05em; background: transparent;"
            )
            val = QLabel("—")
            val.setStyleSheet(
                f"color: {COLOR_TEXT}; font-size: 13px; font-weight: 700; "
                f"font-family: Consolas, monospace; background: transparent;"
            )
            col = QVBoxLayout()
            col.setSpacing(1)
            col.setContentsMargins(0, 0, 0, 0)
            col.addWidget(lbl)
            col.addWidget(val)
            stats_layout.addLayout(col)
            return lbl, val

        _, self._stat_toks   = _stat_pair("tok/s")
        _, self._stat_tokens = _stat_pair("tokens")
        _, self._stat_time   = _stat_pair("elapsed")
        _, self._stat_ttft   = _stat_pair("TTFT")
        _, self._stat_status = _stat_pair("estado")
        stats_layout.addStretch()
        layout.addWidget(stats_bar)





        eval_bar = QFrame()
        eval_bar.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE_RAISED}; "
            f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; }}"
        )
        eval_layout = QVBoxLayout(eval_bar)
        eval_layout.setContentsMargins(12, 6, 12, 6)
        eval_layout.setSpacing(4)

        eval_title_row = QHBoxLayout()
        eval_title_lbl = QLabel("Evaluador de capítulo")
        eval_title_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 10px; font-weight: 600; "
            "text-transform: uppercase; letter-spacing: 0.05em; background: transparent;"
        )
        eval_title_row.addWidget(eval_title_lbl)
        eval_title_row.addStretch()
        self._eval_chapter_lbl = QLabel("")
        self._eval_chapter_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 11px; font-family: Consolas, monospace; "
            "background: transparent;"
        )
        eval_title_row.addWidget(self._eval_chapter_lbl)
        eval_layout.addLayout(eval_title_row)


        self._eval_attempt_rows: list[QLabel] = []
        for _ in range(3):
            row_lbl = QLabel("")
            row_lbl.setStyleSheet(
                f"color: {COLOR_TEXT}; font-size: 12px; font-family: Consolas, monospace; "
                "background: transparent; padding-left: 8px;"
            )
            eval_layout.addWidget(row_lbl)
            self._eval_attempt_rows.append(row_lbl)


        self._eval_verdict_lbl = QLabel("")
        self._eval_verdict_lbl.setStyleSheet(
            f"font-size: 13px; font-weight: 700; font-family: Consolas, monospace; "
            "background: transparent; padding-left: 8px;"
        )
        eval_layout.addWidget(self._eval_verdict_lbl)

        self._eval_bar = eval_bar
        layout.addWidget(eval_bar)


        header = QHBoxLayout()
        title = QLabel("Console")
        title.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {COLOR_TEXT};")
        header.addWidget(title)
        header.addStretch(1)

        self.autoscroll_check = QCheckBox("Auto-scroll")
        self.autoscroll_check.setChecked(True)
        header.addWidget(self.autoscroll_check)

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setToolTip(
            "Pause live updates without losing anything — paused lines are "
            "held and flushed in when you resume."
        )
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        header.addWidget(self.pause_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear)
        header.addWidget(clear_btn)

        layout.addLayout(header)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        console_font = QFont("Consolas")
        console_font.setStyleHint(QFont.Monospace)
        console_font.setPointSize(10)
        self.text.setFont(console_font)
        self.text.setStyleSheet(
            f"""
            QPlainTextEdit {{
                background-color: {COLOR_SURFACE};
                color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER};
                border-radius: 6px;
                padding: 8px;
            }}
            """
        )
        layout.addWidget(self.text, 1)

        hint = QLabel(
            "Shows everything the terminal shows. Enable Settings → "
            "\"Show full prompt sent to the model in the console/log\" to "
            "also see the exact text sent to the model on every call."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        layout.addWidget(hint)

        self._handler = QtLogHandler(level=logging.INFO)
        self._handler.bridge.new_line.connect(self._append_line)

        root_logger = logging.getLogger()
        root_logger.addHandler(self._handler)




        llm_logger = logging.getLogger("llm_engine")
        llm_logger.addHandler(self._handler)

    def _on_pause_toggled(self, checked: bool) -> None:
        self._paused = checked
        self.pause_btn.setText("Resume" if checked else "Pause")
        if not checked and self._pending_while_paused:
            self.text.appendPlainText("\n".join(self._pending_while_paused))
            self._pending_while_paused.clear()
            self._trim_if_needed()
            if self.autoscroll_check.isChecked():
                self._scroll_to_bottom()

    def _append_line(self, line: str) -> None:


        m = _TOK_RE.search(line)
        if m:
            self._update_stats(m)
        self._check_eval_line(line)

        if self._paused:
            self._pending_while_paused.append(line)


            if len(self._pending_while_paused) > MAX_LINES:
                self._pending_while_paused = self._pending_while_paused[-TRIM_TO:]
            return
        self.text.appendPlainText(line)
        self._trim_if_needed()
        if self.autoscroll_check.isChecked():
            self._scroll_to_bottom()

    def _check_eval_line(self, line: str) -> None:
        """Parse a single log line and update the eval panel if it's relevant."""
        m = _EVAL_START_RE.search(line)
        if m:
            self._eval_chapter_lbl.setText(
                f"Cap. {m.group('ch')}  pass {m.group('pass')}  "
                f"({m.group('words')} palabras)"
            )

            for row in self._eval_attempt_rows:
                row.setText("")
            self._eval_verdict_lbl.setText("")
            return

        m = _EVAL_ATTEMPT_RE.search(line)
        if m:
            idx = int(m.group("n")) - 1
            if 0 <= idx < len(self._eval_attempt_rows):
                self._eval_attempt_rows[idx].setText(
                    f"  Intento {m.group('n')}/{m.group('total')}  →  consultando modelo…"
                )
            return

        m = _EVAL_VERDICT_RE.search(line)
        if m:
            raw     = m.group("raw").strip("'\"")
            verdict = m.group("verdict")
            is_true = verdict == "TRUE"
            color   = "#4ec97b" if is_true else COLOR_WARNING

            slot = 0
            for i, row in enumerate(self._eval_attempt_rows):
                if row.text():
                    slot = i
            self._eval_attempt_rows[slot].setStyleSheet(
                f"color: {color}; font-size: 12px; font-family: Consolas, monospace; "
                "background: transparent; padding-left: 8px;"
            )
            self._eval_attempt_rows[slot].setText(
                f"  → modelo: {raw!r}  ({'✓ true' if is_true else '✗ false'})"
            )
            return

        m = _EVAL_UNPARSEABLE_RE.search(line)
        if m:
            raw = m.group("raw").strip()
            slot = 0
            for i, row in enumerate(self._eval_attempt_rows):
                if row.text():
                    slot = i
            self._eval_attempt_rows[slot].setStyleSheet(
                f"color: #e05c5c; font-size: 12px; font-family: Consolas, monospace; "
                "background: transparent; padding-left: 8px;"
            )
            self._eval_attempt_rows[slot].setText(f"  → ⚠ imparseable: {raw}")
            return

        if _EVAL_ALLFAILED_RE.search(line):
            self._eval_verdict_lbl.setStyleSheet(
                "color: #e05c5c; font-size: 13px; font-weight: 700; "
                "font-family: Consolas, monospace; background: transparent; padding-left: 8px;"
            )
            self._eval_verdict_lbl.setText("⚠ Todos los intentos fallaron → FALSE (continuará)")
            return

        m = _EVAL_RESULT_RE.search(line)
        if m:
            action   = m.group("action")
            is_stop  = "STOP" in action or "complete" in action.lower()
            color    = "#4ec97b" if is_stop else COLOR_WARNING
            icon     = "✓" if is_stop else "→"
            self._eval_verdict_lbl.setStyleSheet(
                f"color: {color}; font-size: 13px; font-weight: 700; "
                "font-family: Consolas, monospace; background: transparent; padding-left: 8px;"
            )
            self._eval_verdict_lbl.setText(f"{icon}  {action}")

    def _update_stats(self, m: re.Match) -> None:
        status   = m.group("status")
        tokens   = int(m.group("tokens"))
        elapsed  = float(m.group("elapsed"))
        toks     = float(m.group("toks"))
        ttft_ms  = float(m.group("ttft"))


        if toks >= 10:
            color = "#4ec97b"
        elif toks >= 5:
            color = COLOR_WARNING
        else:
            color = "#e05c5c"

        self._stat_toks.setText(f"{toks:.1f}")
        self._stat_toks.setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: 700; "
            "font-family: Consolas, monospace; background: transparent;"
        )
        self._stat_tokens.setText(str(tokens))
        self._stat_time.setText(f"{elapsed:.1f}s")
        self._stat_ttft.setText(f"{ttft_ms:.0f} ms")
        self._stat_status.setText(status)

    def _trim_if_needed(self) -> None:
        doc = self.text.document()
        if doc.blockCount() <= MAX_LINES:
            return
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.Start)
        lines_to_drop = doc.blockCount() - TRIM_TO
        cursor.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor, lines_to_drop)
        cursor.select(QTextCursor.BlockUnderCursor)
        cursor.removeSelectedText()
        cursor.deleteChar()

    def _scroll_to_bottom(self) -> None:
        bar = self.text.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _clear(self) -> None:
        self.text.clear()
        self._pending_while_paused.clear()

    def closeEvent(self, event) -> None:  # pragma: no cover - UI cleanup
        logging.getLogger().removeHandler(self._handler)
        logging.getLogger("llm_engine").removeHandler(self._handler)
        super().closeEvent(event)
