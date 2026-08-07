"""
Small shared custom widgets used across the UI.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


class SizeAdjustingTabWidget(QTabWidget):
    """
    A QTabWidget whose size hints only reflect the CURRENTLY visible page
    (plus the tab bar itself), instead of the maximum across ALL pages.

    Qt's default QTabWidget/QStackedWidget reports its sizeHint() and
    minimumSizeHint() as the maximum across ALL pages, including hidden
    ones — so the window's minimum size balloons to fit whichever tab has
    the most content (e.g. Chapters, once a project with a long chapter
    is loaded) and can never shrink back down even while viewing a small
    tab like Synopsis. Overriding both hints to only consider the current
    page fixes this: the window resizes/shrinks based on what's actually
    on screen, exactly like a normal (non-tabbed) widget would.
    """

    def _combine_with_tab_bar(self, page_size: QSize) -> QSize:
        bar = self.tabBar()
        if bar is None or not bar.isVisible():
            return page_size
        bar_hint = bar.sizeHint()
        if self.tabPosition() in (QTabWidget.North, QTabWidget.South):
            return QSize(
                max(page_size.width(), bar_hint.width()),
                page_size.height() + bar_hint.height(),
            )
        else:
            return QSize(
                page_size.width() + bar_hint.width(),
                max(page_size.height(), bar_hint.height()),
            )

    def sizeHint(self):
        current = self.currentWidget()
        if current is None:
            return super().sizeHint()
        return self._combine_with_tab_bar(current.sizeHint())

    def minimumSizeHint(self):
        current = self.currentWidget()
        if current is None:
            return super().minimumSizeHint()
        return self._combine_with_tab_bar(current.minimumSizeHint())

    def setCurrentIndex(self, index: int) -> None:
        super().setCurrentIndex(index)
        # Force the layout system to re-query our (now different) size
        # hints immediately, so switching tabs promptly relaxes/tightens
        # the window's minimum size instead of waiting for some other
        # event to trigger a relayout.
        self.updateGeometry()


class EmptyStateCard(QWidget):
    """
    Friendly placeholder shown in place of an empty section: an icon, a
    short explanation of what the section is for, and up to two actions —
    a primary CTA (usually "Generate …") and an optional secondary,
    link-styled action (e.g. "Write it myself").

    Used across the Story tabs so a section with no content yet reads as
    "here's what happens next" instead of just being a blank box.
    Centers itself in whatever space its parent layout gives it.
    """

    primary_clicked = Signal()
    secondary_clicked = Signal()

    def __init__(
        self,
        icon: str = "",
        title: str = "",
        description: str = "",
        primary_label: str = "",
        secondary_label: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setAlignment(Qt.AlignCenter)

        card = QFrame()
        card.setObjectName("emptyStateCard")
        card.setMaximumWidth(440)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(36, 32, 36, 32)
        card_layout.setSpacing(8)
        card_layout.setAlignment(Qt.AlignCenter)

        if icon:
            icon_lbl = QLabel(icon)
            icon_lbl.setObjectName("emptyStateIcon")
            icon_lbl.setAlignment(Qt.AlignCenter)
            card_layout.addWidget(icon_lbl)

        if title:
            title_lbl = QLabel(title)
            title_lbl.setObjectName("emptyStateTitle")
            title_lbl.setAlignment(Qt.AlignCenter)
            title_lbl.setWordWrap(True)
            card_layout.addWidget(title_lbl)

        if description:
            desc_lbl = QLabel(description)
            desc_lbl.setObjectName("emptyStateDesc")
            desc_lbl.setAlignment(Qt.AlignCenter)
            desc_lbl.setWordWrap(True)
            card_layout.addWidget(desc_lbl)

        if primary_label:
            card_layout.addSpacing(8)
            primary_btn = QPushButton(primary_label)
            primary_btn.setObjectName("emptyStateCta")
            primary_btn.setCursor(Qt.PointingHandCursor)
            primary_btn.clicked.connect(self.primary_clicked)
            btn_row = QHBoxLayout()
            btn_row.addStretch()
            btn_row.addWidget(primary_btn)
            btn_row.addStretch()
            card_layout.addLayout(btn_row)

        if secondary_label:
            secondary_btn = QPushButton(secondary_label)
            secondary_btn.setObjectName("emptyStateLink")
            secondary_btn.setCursor(Qt.PointingHandCursor)
            secondary_btn.clicked.connect(self.secondary_clicked)
            link_row = QHBoxLayout()
            link_row.addStretch()
            link_row.addWidget(secondary_btn)
            link_row.addStretch()
            card_layout.addLayout(link_row)

        outer.addWidget(card, 0, Qt.AlignCenter)

