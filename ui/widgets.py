"""
Small shared custom widgets used across the UI.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QTabWidget


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

