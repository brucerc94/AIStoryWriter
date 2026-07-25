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
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from engine import storage
from engine.models import AppSettings, Project, TaskType
from ui.chat import ChatPanel
from ui.projects import ProjectsPanel
from ui.settings import ModelsPanel, SettingsPanel
from ui.story import StoryPanel
from ui.styles import COLOR_BORDER, COLOR_SURFACE, COLOR_TEXT_DIM, COLOR_TEXT_MUTED

logger = logging.getLogger("ui.main")


class EmptyStateWidget(QWidget):
    """Shown in the right pane when no project is open yet."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        lbl = QLabel(
            "Select a project on the left, or click + to create a new one to start writing."
        )
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(420)
        lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 14px; padding: 40px;")
        layout.addWidget(lbl)


class MainWindow(QMainWindow):
    """The application's main window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI Story Studio")
        self.resize(1400, 900)

        self._settings: AppSettings = storage.load_settings()
        self._current_project: Optional[Project] = None

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

        self.project_title_bar = QLabel("")
        self.project_title_bar.setFixedHeight(40)
        self.project_title_bar.setStyleSheet(
            f"background: {COLOR_SURFACE}; color: {COLOR_TEXT_DIM}; "
            f"font-size: 14px; font-weight: 600; padding: 0 16px; "
            f"border-bottom: 1px solid {COLOR_BORDER};"
        )
        right_layout.addWidget(self.project_title_bar)

        self.tabs = QTabWidget()
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
        self.splitter.setSizes([300, 1100])

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
        self.projects_panel.refresh()

    def _run_task(self, task: TaskType, extra_input: str) -> None:
        if not self._current_project:
            return
        logger.info(f"Task requested: {task.value} (project='{self._current_project.title}')")
        self.tabs.setCurrentWidget(self.chat_panel)
        self.chat_panel.run_task(task, extra_input)

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
        self.tabs.hide()
        self.empty_state.show()

    def _show_workspace(self) -> None:
        self.empty_state.hide()
        self.tabs.show()
