"""
Search Tab.

Plain-text search across all project content: chapters, synopsis, outline,
characters, world notes, and story memory. No model, no network — instant
results from in-memory text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from engine.models import Project
from ui.styles import (
    COLOR_ACCENT,
    COLOR_ACCENT_DIM,
    COLOR_BORDER,
    COLOR_BORDER_LIGHT,
    COLOR_SURFACE,
    COLOR_SURFACE_RAISED,
    COLOR_TEXT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
)

_CONTEXT_CHARS = 140
_MAX_RESULTS   = 200




@dataclass
class _Match:
    source: str
    snippet: str
    sort_key: tuple




class _ResultCard(QFrame):
    def __init__(self, source: str, snippet_html: str, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: 8px;
                margin-bottom: 2px;
            }}
            QFrame:hover {{
                border-color: {COLOR_ACCENT};
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        source_lbl = QLabel(source)
        source_lbl.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-size: 11px; font-weight: 700; "
            "text-transform: uppercase; letter-spacing: 0.04em; background: transparent;"
        )
        layout.addWidget(source_lbl)

        snippet_lbl = QLabel()
        snippet_lbl.setText(snippet_html)
        snippet_lbl.setTextFormat(Qt.RichText)
        snippet_lbl.setWordWrap(True)
        snippet_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 13px; background: transparent; line-height: 1.5;"
        )
        layout.addWidget(snippet_lbl)




class SearchTab(QWidget):
    """
    Full-text search across all project content.
    Results update ~300 ms after the user stops typing.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._run_search)
        self._build_ui()



    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)


        bar = QHBoxLayout()
        bar.setSpacing(8)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search chapters, synopsis, outline, characters, world, memory…")
        self._search_input.setMinimumHeight(38)
        self._search_input.setStyleSheet(f"""
            QLineEdit {{
                background: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: 8px;
                padding: 0 14px;
                font-size: 14px;
                color: {COLOR_TEXT};
            }}
            QLineEdit:focus {{
                border-color: {COLOR_ACCENT};
            }}
        """)
        self._search_input.textChanged.connect(self._on_text_changed)
        bar.addWidget(self._search_input, 1)

        clear_btn = QPushButton("✕")
        clear_btn.setFixedSize(38, 38)
        clear_btn.setToolTip("Clear search")
        clear_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: 8px;
                color: {COLOR_TEXT_DIM};
                font-size: 14px;
            }}
            QPushButton:hover {{
                border-color: {COLOR_ACCENT};
                color: {COLOR_TEXT};
            }}
        """)
        clear_btn.clicked.connect(self._search_input.clear)
        bar.addWidget(clear_btn)

        root.addLayout(bar)


        opts = QHBoxLayout()
        opts.setSpacing(12)

        self._case_btn = QPushButton("Aa  Case sensitive")
        self._case_btn.setCheckable(True)
        self._case_btn.setChecked(False)
        self._case_btn.setFixedHeight(28)
        self._case_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: 1px solid {COLOR_BORDER};
                border-radius: 6px;
                color: {COLOR_TEXT_DIM};
                font-size: 12px;
                padding: 0 12px;
            }}
            QPushButton:checked {{
                background: {COLOR_ACCENT_DIM};
                border-color: {COLOR_ACCENT};
                color: {COLOR_TEXT};
            }}
            QPushButton:hover {{
                border-color: {COLOR_ACCENT};
            }}
        """)
        self._case_btn.toggled.connect(lambda _: self._run_search())
        opts.addWidget(self._case_btn)

        self._regex_btn = QPushButton(".*  Regex")
        self._regex_btn.setCheckable(True)
        self._regex_btn.setChecked(False)
        self._regex_btn.setFixedHeight(28)
        self._regex_btn.setStyleSheet(self._case_btn.styleSheet())
        self._regex_btn.toggled.connect(lambda _: self._run_search())
        opts.addWidget(self._regex_btn)

        opts.addStretch()

        self._count_label = QLabel("")
        self._count_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; background: transparent;"
        )
        opts.addWidget(self._count_label)
        root.addLayout(opts)


        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)

        self._results_container = QWidget()
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setContentsMargins(0, 0, 4, 0)
        self._results_layout.setSpacing(6)
        self._results_layout.addStretch()

        self._scroll.setWidget(self._results_container)
        root.addWidget(self._scroll, 1)

        self._show_placeholder("Start typing to search your project.")



    def load(self, project: Project) -> None:
        self._project = project


        if self._search_input.text().strip():
            self._run_search()



    def _on_text_changed(self, text: str) -> None:
        if not text.strip():
            self._clear_results()
            self._show_placeholder("Start typing to search your project.")
            self._count_label.setText("")
            return
        self._debounce.start()

    def _run_search(self) -> None:
        query = self._search_input.text().strip()
        if not query or not self._project:
            return

        self._clear_results()

        case    = self._case_btn.isChecked()
        use_re  = self._regex_btn.isChecked()
        flags   = 0 if case else re.IGNORECASE

        try:
            pattern = re.compile(query if use_re else re.escape(query), flags)
        except re.error as e:
            self._show_placeholder(f"Invalid regex: {e}")
            self._count_label.setText("")
            return

        matches: list[_Match] = []
        p = self._project


        sources: list[tuple[str, str, int]] = []

        sources.append(("Synopsis", p.synopsis, 0))
        sources.append(("Outline", p.outline, 1))
        sources.append(("World & Setting", p.world, 2))
        sources.append(("Story Memory", p.memory, 3))

        for ch in sorted(p.chapters, key=lambda c: c.number):
            sources.append((f"Chapter {ch.number}: {ch.title}", ch.content, 100 + ch.number))

        for ch in p.characters:
            blob = "\n".join(filter(None, [
                ch.description, ch.backstory, " ".join(ch.traits)
            ]))
            sources.append((f"Character: {ch.name}", blob, 50))

        for sort_order, (name, text, order) in enumerate(sources):
            if not text.strip():
                continue
            for m in pattern.finditer(text):
                start = max(0, m.start() - _CONTEXT_CHARS // 2)
                end   = min(len(text), m.end() + _CONTEXT_CHARS // 2)
                snippet = ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")

                snippet_html = self._highlight(snippet, pattern)
                matches.append(_Match(
                    source=name,
                    snippet=snippet_html,
                    sort_key=(order, m.start()),
                ))
                if len(matches) >= _MAX_RESULTS:
                    break
            if len(matches) >= _MAX_RESULTS:
                break


        matches.sort(key=lambda x: x.sort_key)

        if not matches:
            self._show_placeholder("No results found.")
            self._count_label.setText("0 results")
            return

        suffix = f"(showing first {_MAX_RESULTS})" if len(matches) >= _MAX_RESULTS else ""
        self._count_label.setText(f"{len(matches)} result{'s' if len(matches) != 1 else ''} {suffix}".strip())

        for m in matches:
            card = _ResultCard(m.source, m.snippet)
            self._results_layout.insertWidget(self._results_layout.count() - 1, card)

    @staticmethod
    def _highlight(text: str, pattern: re.Pattern) -> str:
        """Wrap each match in a bold+accent-colored span for display."""
        def replace(m: re.Match) -> str:
            escaped = (
                m.group(0)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            return f'<b style="color: {COLOR_ACCENT};">{escaped}</b>'

        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")



        try:
            return pattern.sub(replace, text)
        except Exception:
            return safe

    def _clear_results(self) -> None:
        while self._results_layout.count() > 1:
            item = self._results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _show_placeholder(self, msg: str) -> None:
        lbl = QLabel(msg)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 14px; padding: 40px; background: transparent;"
        )

        self._results_layout.insertWidget(0, lbl)
