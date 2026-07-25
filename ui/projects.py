"""
Projects Panel.

Left sidebar listing all projects with options to create, rename, delete, and open.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from engine import storage
from engine.models import Project
from ui.styles import (
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_SURFACE,
    COLOR_SURFACE_RAISED,
    COLOR_TEXT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
)


class ProjectListItem(QListWidgetItem):
    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project_id = project.id
        self._update(project)

    def _update(self, project: Project) -> None:
        try:
            dt = datetime.fromisoformat(project.updated_at)
            date_str = dt.strftime("%b %d, %Y")
        except Exception:
            date_str = ""

        chapters = len(project.chapters)
        ch_str = f"{chapters} chapter{'s' if chapters != 1 else ''}"

        self.setText(project.title)
        self.setToolTip(f"{project.title} · {ch_str} · {date_str}")
        self.setData(Qt.UserRole, project.id)
        self.setData(Qt.UserRole + 1, project.title)


class ProjectsPanel(QWidget):
    """
    Left sidebar — project browser.
    Emits project_selected(project_id) when user clicks a project.
    """

    project_selected = Signal(str)   # project_id
    project_created = Signal(str)    # project_id
    project_deleted = Signal(str)    # project_id

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(220)
        self.setMaximumWidth(320)
        self._projects: dict[str, Project] = {}
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QFrame()
        header.setStyleSheet(f"background: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_BORDER};")
        header.setFixedHeight(50)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(14, 0, 10, 0)

        title_lbl = QLabel("Projects")
        title_lbl.setStyleSheet(f"color: {COLOR_TEXT}; font-weight: 700; font-size: 14px; background: transparent;")
        h_layout.addWidget(title_lbl)
        h_layout.addStretch()

        self.new_btn = QPushButton("+")
        self.new_btn.setObjectName("accent")
        self.new_btn.setFixedSize(28, 28)
        self.new_btn.setToolTip("New project")
        self.new_btn.clicked.connect(self._create_project)
        h_layout.addWidget(self.new_btn)

        layout.addWidget(header)

        # Search
        search_frame = QFrame()
        search_frame.setStyleSheet(f"background: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_BORDER};")
        s_layout = QHBoxLayout(search_frame)
        s_layout.setContentsMargins(10, 8, 10, 8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search projects…")
        self.search_input.textChanged.connect(self._filter)
        s_layout.addWidget(self.search_input)
        layout.addWidget(search_frame)

        # List
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet(f"""
            QListWidget {{
                background: {COLOR_SURFACE};
                border: none;
                border-radius: 0;
                padding: 4px;
            }}
            QListWidget::item {{
                padding: 10px 12px;
                border-radius: 6px;
                font-size: 13px;
            }}
        """)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._context_menu)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        self.list_widget.itemDoubleClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list_widget, 1)

        # Empty state
        self.empty_label = QLabel("No projects yet.\nClick + to create one.")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 13px; padding: 20px;")
        self.empty_label.hide()
        layout.addWidget(self.empty_label)

    def refresh(self) -> None:
        projects = storage.load_all_projects()
        self._projects = {p.id: p for p in projects}
        self._populate(projects)

    def _populate(self, projects: list[Project]) -> None:
        self.list_widget.clear()
        query = self.search_input.text().strip().lower()
        visible = [p for p in projects if not query or query in p.title.lower()]

        for proj in visible:
            item = ProjectListItem(proj)
            self.list_widget.addItem(item)

        has_items = len(visible) > 0
        self.list_widget.setVisible(has_items)
        self.empty_label.setVisible(not has_items)

    def _filter(self, text: str) -> None:
        projects = list(self._projects.values())
        projects.sort(key=lambda p: p.updated_at, reverse=True)
        self._populate(projects)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        pid = item.data(Qt.UserRole)
        if pid:
            self.project_selected.emit(pid)

    def _context_menu(self, pos) -> None:
        item = self.list_widget.itemAt(pos)
        if not item:
            return
        pid = item.data(Qt.UserRole)
        title = item.data(Qt.UserRole + 1)

        menu = QMenu(self)
        open_action = menu.addAction("Open")
        rename_action = menu.addAction("Rename…")
        menu.addSeparator()
        delete_action = menu.addAction("Delete…")

        action = menu.exec(self.list_widget.mapToGlobal(pos))
        if action == open_action:
            self.project_selected.emit(pid)
        elif action == rename_action:
            self._rename_project(pid, title)
        elif action == delete_action:
            self._delete_project(pid, title)

    def _create_project(self) -> None:
        dialog = NewProjectDialog(self)
        if dialog.exec() == QDialog.Accepted:
            title = dialog.title_input.text().strip()
            if not title:
                return
            proj = Project(title=title)
            if dialog.synopsis_input.toPlainText().strip():
                proj.synopsis = dialog.synopsis_input.toPlainText().strip()
            storage.save_project(proj)
            self._projects[proj.id] = proj
            self.refresh()
            self.project_created.emit(proj.id)
            # Select it
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                if item.data(Qt.UserRole) == proj.id:
                    self.list_widget.setCurrentItem(item)
                    break

    def _rename_project(self, project_id: str, current_title: str) -> None:
        new_title, ok = QInputDialog.getText(
            self, "Rename Project", "New title:", QLineEdit.Normal, current_title
        )
        if ok and new_title.strip():
            storage.rename_project(project_id, new_title.strip())
            self.refresh()

    def _delete_project(self, project_id: str, title: str) -> None:
        reply = QMessageBox.question(
            self,
            "Delete Project",
            f"Permanently delete '{title}'?\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.Cancel,
        )
        if reply == QMessageBox.Yes:
            storage.delete_project(project_id)
            if project_id in self._projects:
                del self._projects[project_id]
            self.refresh()
            self.project_deleted.emit(project_id)

    def select_project(self, project_id: str) -> None:
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.UserRole) == project_id:
                self.list_widget.setCurrentItem(item)
                return


class NewProjectDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Project")
        self.setMinimumWidth(420)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        layout.addWidget(QLabel("Project Title"))
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("e.g. The Midnight Chronicle")
        layout.addWidget(self.title_input)

        layout.addWidget(QLabel("Initial Synopsis (optional)"))
        self.synopsis_input = QTextEdit_small()
        self.synopsis_input.setPlaceholderText(
            "Brief description of your story idea…"
        )
        self.synopsis_input.setFixedHeight(100)
        layout.addWidget(self.synopsis_input)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.title_input.setFocus()

    def accept(self) -> None:
        if not self.title_input.text().strip():
            QMessageBox.warning(self, "Title Required", "Please enter a project title.")
            return
        super().accept()


# Small local import alias to avoid circular issues
from PySide6.QtWidgets import QTextEdit as QTextEdit_small
