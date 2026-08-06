"""
Main Window.

Top-level application window. Wires together:
  - ProjectsPanel (left sidebar)
  - StoryPanel / ChatPanel / ModelsPanel / SettingsPanel (right, tabbed)

Owns the single "current project" and keeps every panel in sync with it.
Nothing here talks to the LLM directly — that's the job of WorkflowThread,
which ChatPanel owns and drives.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from agents.manager import ManagerAgent
from engine import storage
from engine import export as book_export
from engine.models import AppSettings, Project, TaskType
from ui.chat import ChatPanel
from ui.projects import ProjectsPanel
from ui.settings import ModelsPanel, SettingsPanel
from ui.story import StoryPanel
from ui.styles import (
    COLOR_ACCENT,
    COLOR_ACCENT_DIM,
    COLOR_ACCENT_HOVER,
    COLOR_BORDER,
    COLOR_SURFACE,
    COLOR_TEXT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
)
from ui.widgets import SizeAdjustingTabWidget

logger = logging.getLogger("ui.main")


class EmptyStateWidget(QWidget):
    """Shown in the right pane when no project is open yet."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        lbl = QLabel(
            "Selecciona un proyecto a la izquierda\no haz clic en \"+ New\" para crear uno nuevo."
        )
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(460)
        lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 16px; line-height: 1.6; padding: 40px;")
        layout.addWidget(lbl)


class MainWindow(QMainWindow):
    """The application's main window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI Story Studio")
        self.resize(1500, 950)
        self.setMinimumSize(1000, 650)

        self._settings: AppSettings = storage.load_settings()
        self._current_project: Optional[Project] = None
        self._manager = ManagerAgent()
        self._suggested_task: Optional[TaskType] = None

        self._build_ui()
        self._wire_signals()

        # Apply loaded app settings to the panels that need them
        self.settings_panel.load(self._settings)
        self.models_panel.update_available_models(self._settings.models_directory)
        self.chat_panel.set_settings(self._settings)

        self.projects_panel.refresh()
        self._show_empty_state()

    # ──────────────────────────────────────────────
    # UI construction
    # ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.splitter = QSplitter(Qt.Horizontal)

        # Left: projects sidebar
        self.projects_panel = ProjectsPanel()
        self.splitter.addWidget(self.projects_panel)

        # Right: title bar + tabbed workspace (or empty state)
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        title_bar = QFrame()
        title_bar.setFixedHeight(52)
        title_bar.setStyleSheet(
            f"background: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_BORDER};"
        )
        title_bar_layout = QHBoxLayout(title_bar)
        title_bar_layout.setContentsMargins(20, 0, 16, 0)

        self.project_title_bar = QLabel("")
        self.project_title_bar.setStyleSheet(
            f"color: {COLOR_TEXT}; font-size: 16px; font-weight: 700; "
            "background: transparent;"
        )
        title_bar_layout.addWidget(self.project_title_bar)
        title_bar_layout.addStretch()

        self.next_step_btn = QPushButton("")
        self.next_step_btn.setObjectName("subtle")
        self.next_step_btn.setMinimumHeight(34)
        self.next_step_btn.setStyleSheet(
            f"QPushButton#subtle {{ color: {COLOR_ACCENT}; font-size: 14px; font-weight: 600; padding: 0 14px; }}"
            f"QPushButton#subtle:hover {{ color: {COLOR_ACCENT_HOVER}; background: {COLOR_ACCENT_DIM}; border-radius: 6px; }}"
        )
        self.next_step_btn.setToolTip(
            "Suggested next step in the synopsis → outline → characters/world → "
            "chapters → memory flow. Click to jump there."
        )
        self.next_step_btn.clicked.connect(self._go_to_suggested_step)
        self.next_step_btn.hide()
        title_bar_layout.addWidget(self.next_step_btn)

        self.export_btn = QPushButton("⇩  Export")
        self.export_btn.setObjectName("subtle")
        self.export_btn.setMinimumHeight(34)
        self.export_btn.setStyleSheet(
            f"QPushButton#subtle {{ font-size: 14px; padding: 0 14px; color: {COLOR_TEXT_DIM}; }}"
            f"QPushButton#subtle:hover {{ color: {COLOR_TEXT}; }}"
        )
        self.export_btn.setToolTip("Exportar la novela completa a Word o PDF")
        self.export_btn.clicked.connect(self._show_export_menu)
        title_bar_layout.addWidget(self.export_btn)

        right_layout.addWidget(title_bar)

        self.tabs = SizeAdjustingTabWidget()
        self.tabs.setDocumentMode(True)

        self.story_panel = StoryPanel()
        self.tabs.addTab(self.story_panel, "Story")

        self.chat_panel = ChatPanel()
        self.tabs.addTab(self.chat_panel, "Chat")

        self.models_panel = ModelsPanel()
        self.tabs.addTab(self.models_panel, "Models")

        self.settings_panel = SettingsPanel()
        self.tabs.addTab(self.settings_panel, "Settings")

        self.empty_state = EmptyStateWidget()

        right_layout.addWidget(self.empty_state, 1)
        right_layout.addWidget(self.tabs, 1)
        self.tabs.hide()

        self.splitter.addWidget(right_container)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([340, 1160])

        root_layout.addWidget(self.splitter)
        self.setCentralWidget(central)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready")

    # ──────────────────────────────────────────────
    # Signal wiring
    # ──────────────────────────────────────────────

    def _wire_signals(self) -> None:
        self.projects_panel.project_selected.connect(self._open_project)
        self.projects_panel.project_created.connect(self._open_project)
        self.projects_panel.project_deleted.connect(self._on_project_deleted)

        self.story_panel.task_requested.connect(self._run_task)
        self.story_panel.project_changed.connect(self._on_project_edited)

        self.chat_panel.project_updated.connect(self._on_ai_task_finished)

        self.models_panel.assignments_changed.connect(self._on_project_edited)

        self.settings_panel.settings_changed.connect(self._on_settings_changed)

    # ──────────────────────────────────────────────
    # Project lifecycle
    # ──────────────────────────────────────────────

    def _open_project(self, project_id: str) -> None:
        project = storage.load_project(project_id)
        if project is None:
            logger.error(f"Could not load project {project_id}")
            self.status.showMessage(f"Could not load project {project_id}", 5000)
            return

        self._current_project = project
        self.story_panel.load_project(project)
        self.chat_panel.load_project(project)
        self.models_panel.load_project(project)

        self.project_title_bar.setText(project.title)
        self.projects_panel.select_project(project_id)
        self._show_workspace()
        self._update_next_step()
        logger.info(f"Opened project '{project.title}' ({project_id})")
        self.status.showMessage(f"Opened '{project.title}'", 3000)

    def _on_project_deleted(self, project_id: str) -> None:
        if self._current_project and self._current_project.id == project_id:
            self._current_project = None
            self._show_empty_state()
        logger.info(f"Project deleted ({project_id})")
        self.status.showMessage("Project deleted", 3000)

    def _on_project_edited(self) -> None:
        """A panel edited project data directly (not via an AI task)."""
        if not self._current_project:
            return
        self.projects_panel.refresh()
        self.project_title_bar.setText(self._current_project.title)
        self._update_next_step()

    def _on_ai_task_finished(self) -> None:
        """An AI workflow task finished and persisted its own changes."""
        if not self._current_project:
            return
        refreshed = storage.load_project(self._current_project.id)
        if refreshed is None:
            return
        self._current_project = refreshed
        self.story_panel.refresh_after_task(refreshed)
        self.models_panel.load_project(refreshed)
        # Keep ChatPanel's project object in sync with everyone else's.
        # Without this, StoryPanel/ModelsPanel end up mutating a *different*
        # in-memory Project object (this freshly reloaded one) than the one
        # ChatPanel actually hands to WorkflowThread — so things like
        # "which chapter number to target" set from the Chapters tab would
        # silently have no effect on the next task, since the thread reads
        # them off ChatPanel's stale copy instead.
        self.chat_panel.sync_project_reference(refreshed)
        self.projects_panel.refresh()
        self._update_next_step()

    def _run_task(self, task: TaskType, extra_input: str) -> None:
        if not self._current_project:
            return
        logger.info(f"Task requested: {task.value} (project='{self._current_project.title}')")
        self.tabs.setCurrentWidget(self.chat_panel)
        self.chat_panel.run_task(task, extra_input)

    # ──────────────────────────────────────────────
    # Suggested next step
    # ──────────────────────────────────────────────

    def _update_next_step(self) -> None:
        """
        Refreshes the "suggested next step" chip in the title bar, using
        the synopsis → outline → chapters → memory flow ManagerAgent
        already knows about. Clicking the chip jumps to where that step
        happens — it never triggers generation on its own, since some
        steps (like the outline) need the author to make a choice first
        (e.g. how many chapters).
        """
        project = self._current_project
        if not project:
            self.next_step_btn.hide()
            self._suggested_task = None
            return
        task, description = self._manager.suggest_workflow_next_step(project)
        self._suggested_task = task
        self.next_step_btn.setText(f"Next: {description} →")
        self.next_step_btn.show()

    def _go_to_suggested_step(self) -> None:
        if not self._current_project or not self._suggested_task:
            return
        task = self._suggested_task
        self.tabs.setCurrentWidget(self.story_panel)
        story_tabs = self.story_panel.tabs
        if task == TaskType.WRITE_SYNOPSIS:
            story_tabs.setCurrentWidget(self.story_panel.synopsis_tab)
        elif task == TaskType.GENERATE_OUTLINE:
            story_tabs.setCurrentWidget(self.story_panel.outline_tab)
        else:
            story_tabs.setCurrentWidget(self.story_panel.chapters_tab)

    # ──────────────────────────────────────────────
    # Export
    # ──────────────────────────────────────────────

    def _show_export_menu(self) -> None:
        if not self._current_project:
            self.status.showMessage("Open a project first.", 3000)
            return

        menu = QMenu(self)
        docx_action = menu.addAction("Export as Word (.docx)…")
        pdf_action = menu.addAction("Export as PDF (.pdf)…")
        chosen = menu.exec(self.export_btn.mapToGlobal(self.export_btn.rect().bottomLeft()))

        if chosen == docx_action:
            self._export_book("docx")
        elif chosen == pdf_action:
            self._export_book("pdf")

    def _export_book(self, fmt: str) -> None:
        project = self._current_project
        if not project:
            return

        safe_title = "".join(
            c for c in project.title if c.isalnum() or c in (" ", "-", "_")
        ).strip() or "novel"

        if fmt == "docx":
            filter_str = "Word Document (*.docx)"
            default_name = f"{safe_title}.docx"
        else:
            filter_str = "PDF Document (*.pdf)"
            default_name = f"{safe_title}.pdf"

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Book", default_name, filter_str
        )
        if not path:
            return

        try:
            if fmt == "docx":
                book_export.export_to_docx(project, path)
            else:
                book_export.export_to_pdf(project, path)
        except RuntimeError as e:
            logger.error(f"Export failed: {e}")
            QMessageBox.warning(self, "Export Failed", str(e))
            return
        except Exception as e:
            logger.error(f"Export failed: {e}")
            QMessageBox.critical(self, "Export Failed", f"Could not export the book:\n{e}")
            return

        logger.info(f"Exported '{project.title}' to {path}")
        self.status.showMessage(f"Exported to {path}", 5000)
        QMessageBox.information(self, "Export Complete", f"Saved to:\n{path}")

    # ──────────────────────────────────────────────
    # Settings
    # ──────────────────────────────────────────────

    def _on_settings_changed(self, settings: AppSettings) -> None:
        self._settings = settings
        self.chat_panel.set_settings(settings)
        self.models_panel.update_available_models(settings.models_directory)
        self.status.showMessage("Settings saved", 3000)

    # ──────────────────────────────────────────────
    # View state
    # ──────────────────────────────────────────────

    def _show_empty_state(self) -> None:
        self.project_title_bar.setText("")
        self.next_step_btn.hide()
        self._suggested_task = None
        self.tabs.hide()
        self.empty_state.show()

    def _show_workspace(self) -> None:
        self.empty_state.hide()
        self.tabs.show()