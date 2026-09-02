"""
Stats Tab.

Shows word counts, chapter progress, review status, and reading-time
estimates for the current project — all computed locally from the project
data, no model needed.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from engine.models import Project
from ui.styles import (
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_BORDER_LIGHT,
    COLOR_SUCCESS,
    COLOR_SURFACE,
    COLOR_SURFACE_RAISED,
    COLOR_TEXT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    COLOR_WARNING,
    FONT_MONO,
)


_WPM = 250


def _word_count(text: str) -> int:
    return len(text.split()) if text.strip() else 0


def _reading_time(words: int) -> str:
    minutes = words / _WPM
    if minutes < 1:
        return "< 1 min"
    if minutes < 60:
        return f"{round(minutes)} min"
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"




class _StatCard(QFrame):
    """A single metric card: big number + label, optional sub-label."""

    def __init__(self, value: str, label: str, sub: str = "", accent: bool = False) -> None:
        super().__init__()
        self.setObjectName("statCard")
        self.setStyleSheet(f"""
            QFrame#statCard {{
                background: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: 10px;
                padding: 4px;
            }}
        """)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(88)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(2)
        layout.setAlignment(Qt.AlignVCenter)

        color = COLOR_ACCENT if accent else COLOR_TEXT
        self._value_lbl = QLabel(value)
        self._value_lbl.setStyleSheet(
            f"font-size: 28px; font-weight: 800; color: {color}; "
            f"font-family: {FONT_MONO}; background: transparent;"
        )
        layout.addWidget(self._value_lbl)

        self._label_lbl = QLabel(label)
        self._label_lbl.setStyleSheet(
            f"font-size: 12px; color: {COLOR_TEXT_DIM}; background: transparent;"
        )
        layout.addWidget(self._label_lbl)

        if sub:
            self._sub_lbl = QLabel(sub)
            self._sub_lbl.setStyleSheet(
                f"font-size: 11px; color: {COLOR_TEXT_MUTED}; background: transparent;"
            )
            layout.addWidget(self._sub_lbl)

    def update_value(self, value: str, sub: str = "") -> None:
        self._value_lbl.setText(value)
        if hasattr(self, "_sub_lbl"):
            self._sub_lbl.setText(sub)




class _ChapterRow(QWidget):
    """One row in the per-chapter breakdown table."""

    def __init__(self, num: int, title: str, words: int, reviewed: bool) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(12)

        num_lbl = QLabel(f"{num:02d}")
        num_lbl.setFixedWidth(28)
        num_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; "
            f"font-family: {FONT_MONO}; background: transparent;"
        )
        layout.addWidget(num_lbl)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 13px; background: transparent;")
        title_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(title_lbl, 1)

        words_lbl = QLabel(f"{words:,} w")
        words_lbl.setFixedWidth(70)
        words_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        words_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 12px; "
            f"font-family: {FONT_MONO}; background: transparent;"
        )
        layout.addWidget(words_lbl)

        status_color = COLOR_SUCCESS if reviewed else COLOR_WARNING
        status_text = "✓ Reviewed" if reviewed else "⬦ Draft"
        status_lbl = QLabel(status_text)
        status_lbl.setFixedWidth(78)
        status_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        status_lbl.setStyleSheet(
            f"color: {status_color}; font-size: 11px; font-weight: 600; background: transparent;"
        )
        layout.addWidget(status_lbl)


        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)




class StatsTab(QWidget):
    """
    Read-only statistics panel for the current project.
    Call load(project) whenever the project changes.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._build_ui()



    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self._layout = QVBoxLayout(inner)
        self._layout.setContentsMargins(24, 20, 24, 28)
        self._layout.setSpacing(20)
        scroll.setWidget(inner)
        root.addWidget(scroll)


        self._grid = QGridLayout()
        self._grid.setSpacing(12)

        self._card_total_words  = _StatCard("—", "Total words", accent=True)
        self._card_chapters     = _StatCard("—", "Chapters written")
        self._card_read_time    = _StatCard("—", "Est. reading time")
        self._card_reviewed     = _StatCard("—", "Chapters reviewed")

        self._grid.addWidget(self._card_total_words, 0, 0)
        self._grid.addWidget(self._card_chapters,    0, 1)
        self._grid.addWidget(self._card_read_time,   0, 2)
        self._grid.addWidget(self._card_reviewed,    0, 3)
        self._layout.addLayout(self._grid)


        progress_section = QWidget()
        ps_layout = QVBoxLayout(progress_section)
        ps_layout.setContentsMargins(0, 0, 0, 0)
        ps_layout.setSpacing(6)

        self._progress_label = QLabel("Draft Progress")
        self._progress_label.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 12px; font-weight: 600; "
            "text-transform: uppercase; letter-spacing: 0.05em; background: transparent;"
        )
        ps_layout.addWidget(self._progress_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("rowProgress")
        self._progress_bar.setFixedHeight(8)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setStyleSheet(f"""
            QProgressBar#rowProgress {{
                background: {COLOR_BORDER};
                border: none;
                border-radius: 4px;
            }}
            QProgressBar#rowProgress::chunk {{
                background: {COLOR_ACCENT};
                border-radius: 4px;
            }}
        """)
        ps_layout.addWidget(self._progress_bar)

        self._progress_detail = QLabel("")
        self._progress_detail.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px; background: transparent;"
        )
        ps_layout.addWidget(self._progress_detail)
        self._layout.addWidget(progress_section)


        section_row = QWidget()
        sr_layout = QHBoxLayout(section_row)
        sr_layout.setContentsMargins(0, 0, 0, 0)
        sr_layout.setSpacing(12)

        self._section_cards: dict[str, _StatCard] = {}
        for key, label in [
            ("synopsis", "Synopsis"),
            ("outline",  "Outline"),
            ("world",    "World notes"),
            ("memory",   "Story memory"),
        ]:
            card = _StatCard("—", label)
            self._section_cards[key] = card
            sr_layout.addWidget(card)

        self._layout.addWidget(section_row)


        breakdown_label = QLabel("Chapter Breakdown")
        breakdown_label.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 12px; font-weight: 600; "
            "text-transform: uppercase; letter-spacing: 0.05em; background: transparent;"
        )
        self._layout.addWidget(breakdown_label)

        self._chapter_table = QFrame()
        self._chapter_table.setStyleSheet(f"""
            QFrame {{
                background: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: 10px;
            }}
        """)
        self._chapter_table_layout = QVBoxLayout(self._chapter_table)
        self._chapter_table_layout.setContentsMargins(0, 4, 0, 4)
        self._chapter_table_layout.setSpacing(0)

        self._empty_chapter_lbl = QLabel("No chapters yet.")
        self._empty_chapter_lbl.setAlignment(Qt.AlignCenter)
        self._empty_chapter_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 13px; padding: 24px; background: transparent;"
        )
        self._chapter_table_layout.addWidget(self._empty_chapter_lbl)
        self._layout.addWidget(self._chapter_table)
        self._layout.addStretch()



    def load(self, project: Project) -> None:
        self._project = project
        self._refresh()

    def _refresh(self) -> None:
        p = self._project
        if not p:
            return

        chapters_written = [c for c in p.chapters if c.content.strip()]
        chapters_reviewed = [c for c in chapters_written if c.reviewed]


        chapter_words = sum(_word_count(c.content) for c in chapters_written)
        synopsis_words = _word_count(p.synopsis)
        outline_words  = _word_count(p.outline)
        world_words    = _word_count(p.world)
        memory_words   = _word_count(p.memory)
        total_words    = chapter_words


        self._card_total_words.update_value(
            f"{total_words:,}",
            sub=_reading_time(total_words),
        )
        self._card_chapters.update_value(
            f"{len(chapters_written)}",
            sub=f"of {len(p.chapters)} total",
        )
        self._card_read_time.update_value(
            _reading_time(total_words),
            sub=f"at {_WPM} wpm",
        )
        self._card_reviewed.update_value(
            f"{len(chapters_reviewed)}",
            sub=f"of {len(chapters_written)} written",
        )


        from ui.story import outline_chapter_numbers
        outline_nums = outline_chapter_numbers(p.outline)
        target = max(outline_nums) if outline_nums else 0
        if target:
            self._progress_bar.setRange(0, target)
            self._progress_bar.setValue(min(len(chapters_written), target))
            pct = round(len(chapters_written) / target * 100)
            self._progress_detail.setText(
                f"{len(chapters_written)} / {target} chapters  ·  {pct}% complete"
            )
        else:
            self._progress_bar.setRange(0, max(len(chapters_written), 1))
            self._progress_bar.setValue(len(chapters_written))
            self._progress_detail.setText(
                "Set a chapter count in the Outline tab to track progress against a target."
            )


        def _fmt(words: int) -> str:
            return f"{words:,} words" if words else "Empty"

        self._section_cards["synopsis"].update_value(
            f"{synopsis_words:,}" if synopsis_words else "—", sub=_fmt(synopsis_words)
        )
        self._section_cards["outline"].update_value(
            f"{outline_words:,}" if outline_words else "—", sub=_fmt(outline_words)
        )
        self._section_cards["world"].update_value(
            f"{world_words:,}" if world_words else "—", sub=_fmt(world_words)
        )
        self._section_cards["memory"].update_value(
            f"{memory_words:,}" if memory_words else "—", sub=_fmt(memory_words)
        )


        while self._chapter_table_layout.count():
            item = self._chapter_table_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        sorted_chapters = sorted(p.chapters, key=lambda c: c.number)
        if not sorted_chapters:
            self._empty_chapter_lbl = QLabel("No chapters yet.")
            self._empty_chapter_lbl.setAlignment(Qt.AlignCenter)
            self._empty_chapter_lbl.setStyleSheet(
                f"color: {COLOR_TEXT_MUTED}; font-size: 13px; padding: 24px; background: transparent;"
            )
            self._chapter_table_layout.addWidget(self._empty_chapter_lbl)
            return


        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 8, 12, 8)
        hl.setSpacing(12)
        for text, width, align in [
            ("#", 28, Qt.AlignLeft),
            ("Title", 0, Qt.AlignLeft),
            ("Words", 70, Qt.AlignRight),
            ("Status", 78, Qt.AlignRight),
        ]:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"color: {COLOR_TEXT_MUTED}; font-size: 11px; font-weight: 700; "
                "text-transform: uppercase; letter-spacing: 0.05em; background: transparent;"
            )
            lbl.setAlignment(align | Qt.AlignVCenter)
            if width:
                lbl.setFixedWidth(width)
            else:
                lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            hl.addWidget(lbl, 1 if not width else 0)
        self._chapter_table_layout.addWidget(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"background: {COLOR_BORDER}; max-height: 1px;")
        self._chapter_table_layout.addWidget(sep)

        for i, ch in enumerate(sorted_chapters):
            row = _ChapterRow(
                ch.number,
                ch.title,
                _word_count(ch.content),
                ch.reviewed,
            )
            if i % 2 == 1:
                row.setStyleSheet(f"background: {COLOR_SURFACE_RAISED}; border-radius: 0;")
            self._chapter_table_layout.addWidget(row)
