"""
Settings Panel.

Two sections:
1. App Settings — models directory, GPU layers, threads, context size, theme
2. Model Assignments — assign a GGUF model to each task type
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from engine import storage
from engine.models import AppSettings, Project, TaskType
from ui.styles import (
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_SURFACE,
    COLOR_SURFACE_RAISED,
    COLOR_TEXT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
)


TASK_LABELS: dict[TaskType, str] = {
    TaskType.CHAT: "Chat / Q&A",
    TaskType.WRITE_SYNOPSIS: "Write Synopsis",
    TaskType.GENERATE_OUTLINE: "Generate Outline",
    TaskType.REVIEW_OUTLINE: "Review Outline",
    TaskType.GENERATE_WORLD: "Generate World",
    TaskType.WRITE_CHAPTER: "Write Chapter",
    TaskType.WRITE_BOOK: "Write Book",
    TaskType.REVIEW_CHAPTER: "Review Chapter",
    TaskType.REWRITE_CHAPTER: "Rewrite Chapter",
    TaskType.UPDATE_MEMORY: "Update Story Memory",
    TaskType.CONVERSATION_SUMMARY: "Conversation Summary",
}


class ModelPicker(QWidget):
    """A row with a task label, a model path combo/file picker, and that
    task's own generation temperature — different models often want very
    different temperatures (fast/precise for summaries vs. large/creative
    for chapters), so it lives right next to the model it applies to."""

    model_changed = Signal(TaskType, str)
    temperature_changed = Signal(TaskType, float)

    def __init__(self, task: TaskType, models: list[str], parent=None) -> None:
        super().__init__(parent)
        self.task = task
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        lbl = QLabel(TASK_LABELS.get(task, task.value))
        lbl.setFixedWidth(160)
        lbl.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 13px;")
        layout.addWidget(lbl)

        self.combo = QComboBox()
        self.combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.combo.addItem("— not assigned —", "")
        for m in models:
            self.combo.addItem(Path(m).name, m)
        self.combo.currentIndexChanged.connect(self._on_changed)
        layout.addWidget(self.combo, 1)

        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(70)
        browse_btn.clicked.connect(self._browse)
        layout.addWidget(browse_btn)

        temp_lbl = QLabel("Temp")
        temp_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        layout.addWidget(temp_lbl)

        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.05)
        self.temp_spin.setDecimals(2)
        self.temp_spin.setValue(0.7)
        self.temp_spin.setFixedWidth(70)
        self.temp_spin.setToolTip(
            f"Generation temperature for {TASK_LABELS.get(task, task.value)}. "
            "0.0 = deterministic/focused, 2.0 = max randomness."
        )
        self.temp_spin.valueChanged.connect(self._on_temperature_changed)
        layout.addWidget(self.temp_spin)

    def set_value(self, path: str) -> None:
        # Find by data
        for i in range(self.combo.count()):
            if self.combo.itemData(i) == path:
                self.combo.setCurrentIndex(i)
                return
        # Not in list — add it
        if path:
            self.combo.addItem(Path(path).name, path)
            self.combo.setCurrentIndex(self.combo.count() - 1)

    def get_value(self) -> str:
        return self.combo.currentData() or ""

    def set_temperature(self, temperature: float) -> None:
        self.temp_spin.blockSignals(True)
        self.temp_spin.setValue(temperature)
        self.temp_spin.blockSignals(False)

    def get_temperature(self) -> float:
        return self.temp_spin.value()

    def _on_temperature_changed(self, value: float) -> None:
        self.temperature_changed.emit(self.task, value)

    def update_models(self, models: list[str]) -> None:
        current = self.get_value()
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem("— not assigned —", "")
        for m in models:
            self.combo.addItem(Path(m).name, m)
        self.combo.blockSignals(False)
        if current:
            self.set_value(current)

    def _on_changed(self) -> None:
        self.model_changed.emit(self.task, self.get_value())

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select GGUF Model",
            "",
            "GGUF Models (*.gguf);;All Files (*)",
        )
        if path:
            # Add to combo if not present
            found = False
            for i in range(self.combo.count()):
                if self.combo.itemData(i) == path:
                    self.combo.setCurrentIndex(i)
                    found = True
                    break
            if not found:
                self.combo.addItem(Path(path).name, path)
                self.combo.setCurrentIndex(self.combo.count() - 1)


class AppSettingsWidget(QWidget):
    settings_changed = Signal(AppSettings)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._settings: Optional[AppSettings] = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        box = QGroupBox("Application")
        form = QFormLayout(box)
        form.setSpacing(10)
        form.setContentsMargins(14, 18, 14, 14)

        # Models directory
        dir_row = QHBoxLayout()
        self.models_dir_input = QLineEdit()
        self.models_dir_input.setPlaceholderText("Path to folder containing .gguf files")
        dir_row.addWidget(self.models_dir_input, 1)
        browse_dir_btn = QPushButton("Browse…")
        browse_dir_btn.setFixedWidth(70)
        browse_dir_btn.clicked.connect(self._browse_models_dir)
        dir_row.addWidget(browse_dir_btn)
        form.addRow("Models Directory", dir_row)

        # Context size
        self.ctx_spin = QSpinBox()
        self.ctx_spin.setRange(512, 131072)
        self.ctx_spin.setSingleStep(512)
        self.ctx_spin.setValue(4096)
        self.ctx_spin.setSuffix(" tokens")
        self.ctx_spin.setToolTip(
            "The model's total window: prompt + reply combined. This is a "
            "hard ceiling set by the model itself — it does not control how "
            "much the app asks the model to write in one go (see \"Max "
            "Tokens per Pass\" below)."
        )
        form.addRow("Context Size", self.ctx_spin)

        # GPU layers
        self.gpu_spin = QSpinBox()
        self.gpu_spin.setRange(0, 999)
        self.gpu_spin.setValue(0)
        self.gpu_spin.setToolTip("Number of layers to offload to GPU. 0 = CPU only.")
        form.addRow("GPU Layers", self.gpu_spin)

        # Threads
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 64)
        self.threads_spin.setValue(4)
        form.addRow("CPU Threads", self.threads_spin)

        # Auto-save
        self.autosave_check = QCheckBox("Auto-save after AI responses")
        self.autosave_check.setChecked(True)
        form.addRow("", self.autosave_check)

        # Qwen thinking mode
        self.enable_thinking_check = QCheckBox("Enable Thinking")
        self.enable_thinking_check.setToolTip(
            "When enabled, Qwen models that support it will receive "
            "chat_template_kwargs={'enable_thinking': True}."
        )
        form.addRow("", self.enable_thinking_check)

        # NSFW / unrestricted content
        self.allow_nsfw_check = QCheckBox("Allow mature / unrestricted content (NSFW)")
        self.allow_nsfw_check.setToolTip(
            "When enabled, the model receives an explicit instruction that it may write "
            "adult content without restrictions — graphic violence, explicit sexuality, "
            "strong language, and dark themes — for the creative writing tasks: synopsis, "
            "outline, chapters, rewrite, world-building, and story memory.\n\n"
            "Has no effect on review, chat, or summary tasks.\n\n"
            "Use this when your model has been uncensored/fine-tuned for adult fiction "
            "and you want it to follow your story without softening scenes."
        )
        form.addRow("", self.allow_nsfw_check)

        layout.addWidget(box)

        # ── Generation settings ──
        gen_box = QGroupBox("Generation")
        gen_form = QFormLayout(gen_box)
        gen_form.setSpacing(10)
        gen_form.setContentsMargins(14, 18, 14, 14)

        # NOTE: temperature now lives per-task in the Models tab, next to
        # each task's assigned GGUF model — different models need very
        # different temperatures, so a single global value here didn't
        # give real control. Only the custom system prompt (which makes
        # sense as one shared value across every task) stays here.

        # Response language — every built-in instruction/prompt in this app
        # is written in English, which otherwise biases local models toward
        # answering in English regardless of what language your story is in.
        self.language_input = QLineEdit()
        self.language_input.setPlaceholderText("e.g. Español, English, Français…")
        self.language_input.setToolTip(
            "The model is told explicitly to respond in this language, "
            "since the app's own built-in instructions are all in English. "
            "Leave blank to not add this instruction."
        )
        gen_form.addRow("Response Language", self.language_input)

        # Max tokens per generation pass — shared by Outline, Write Chapter
        # (and therefore Write Book, which calls it per chapter), and
        # Rewrite Chapter. NOT the same as Context Size above: this is how
        # much reply the app requests in one pass before its continuation
        # loop kicks in if the model stops early (out of tokens) or the
        # content isn't finished yet — Context Size is the model's total
        # window and still caps this from the other side.
        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(256, 32000)
        self.max_tokens_spin.setSingleStep(256)
        self.max_tokens_spin.setValue(4000)
        self.max_tokens_spin.setSuffix(" tokens")
        self.max_tokens_spin.setToolTip(
            "Reply length requested per generation pass for Outline, Write "
            "Chapter, Write Book, and Rewrite Chapter — one shared value, "
            "so they never drift apart. If the model stops before finishing "
            "(runs out of tokens or the outline/chapter isn't complete "
            "yet), the app automatically continues in another pass rather "
            "than saving a partial result.\n\n"
            "This is separate from Context Size: Context Size is the "
            "model's total window (prompt + reply); this is what the app "
            "asks for in a single pass within that window."
        )
        gen_form.addRow("Max Tokens per Pass", self.max_tokens_spin)

        # Custom system prompt — appended to every auto-generated prompt
        self.system_prompt_input = QPlainTextEdit()
        self.system_prompt_input.setPlaceholderText(
            "Optional instructions appended to every system prompt, for every task "
            "(chat, writing, review, memory, summaries).\n\n"
            "Examples:\n"
            "- Tone/POV/style rules the model should always follow\n"
            "- Content rating notes\n"
            "- House style guide reminders"
        )
        self.system_prompt_input.setFixedHeight(140)
        gen_form.addRow("Custom System Prompt", self.system_prompt_input)

        layout.addWidget(gen_box)

        save_btn = QPushButton("Save App Settings")
        save_btn.setObjectName("accent")
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)

    def load(self, settings: AppSettings) -> None:
        self._settings = settings
        self.models_dir_input.setText(settings.models_directory)
        self.ctx_spin.setValue(settings.default_context_size)
        self.gpu_spin.setValue(settings.default_gpu_layers)
        self.threads_spin.setValue(settings.default_threads)
        self.autosave_check.setChecked(settings.auto_save)
        self.enable_thinking_check.setChecked(getattr(settings, "enable_thinking", False))
        self.allow_nsfw_check.setChecked(getattr(settings, "allow_nsfw", False))
        self.language_input.setText(settings.response_language)
        self.max_tokens_spin.setValue(getattr(settings, "content_max_tokens", 4000))
        self.system_prompt_input.setPlainText(settings.custom_system_prompt)

    def _browse_models_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select Models Directory")
        if d:
            self.models_dir_input.setText(d)

    def _save(self) -> None:
        if self._settings is None:
            self._settings = AppSettings()
        self._settings.models_directory = self.models_dir_input.text().strip()
        self._settings.default_context_size = self.ctx_spin.value()
        self._settings.default_gpu_layers = self.gpu_spin.value()
        self._settings.default_threads = self.threads_spin.value()
        self._settings.auto_save = self.autosave_check.isChecked()
        self._settings.enable_thinking = self.enable_thinking_check.isChecked()
        self._settings.allow_nsfw = self.allow_nsfw_check.isChecked()
        self._settings.response_language = self.language_input.text().strip()
        self._settings.content_max_tokens = self.max_tokens_spin.value()
        self._settings.custom_system_prompt = self.system_prompt_input.toPlainText().strip()
        storage.save_settings(self._settings)
        self.settings_changed.emit(self._settings)

    def get_models_directory(self) -> str:
        return self.models_dir_input.text().strip()


class ModelsPanel(QWidget):
    """
    The Models tab — assigns a GGUF model to each task for the current project.
    """

    assignments_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._available_models: list[str] = []
        self._pickers: dict[TaskType, ModelPicker] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(16)

        title = QLabel("Model Assignments")
        title.setObjectName("heading")
        outer.addWidget(title)

        subtitle = QLabel(
            "Each task can use a different GGUF model. "
            "The Manager Agent loads the correct model automatically."
        )
        subtitle.setObjectName("subheading")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        # Refresh models button
        refresh_row = QHBoxLayout()
        self.dir_label = QLabel("No models directory set. Configure in Settings.")
        self.dir_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        refresh_row.addWidget(self.dir_label, 1)
        refresh_btn = QPushButton("Refresh Models")
        refresh_btn.clicked.connect(self._refresh_models)
        refresh_row.addWidget(refresh_btn)
        outer.addLayout(refresh_row)

        # Picker grid
        box = QGroupBox("Task → Model Assignment")
        box_layout = QVBoxLayout(box)
        box_layout.setSpacing(10)
        box_layout.setContentsMargins(14, 18, 14, 14)

        for task in TaskType:
            picker = ModelPicker(task, self._available_models)
            picker.model_changed.connect(self._on_model_changed)
            picker.temperature_changed.connect(self._on_temperature_changed)
            self._pickers[task] = picker
            box_layout.addWidget(picker)

        outer.addWidget(box)

        # Quick actions
        action_box = QGroupBox("Quick Actions")
        ab_layout = QVBoxLayout(action_box)
        ab_layout.setContentsMargins(14, 18, 14, 14)
        ab_layout.setSpacing(8)

        assign_all_row = QHBoxLayout()
        assign_all_lbl = QLabel("Assign one model to all tasks:")
        assign_all_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        assign_all_row.addWidget(assign_all_lbl)
        self.assign_all_combo = QComboBox()
        self.assign_all_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.assign_all_combo.addItem("— select model —", "")
        assign_all_row.addWidget(self.assign_all_combo, 1)
        assign_all_btn = QPushButton("Assign to All")
        assign_all_btn.clicked.connect(self._assign_to_all)
        assign_all_row.addWidget(assign_all_btn)
        ab_layout.addLayout(assign_all_row)

        outer.addWidget(action_box)
        outer.addStretch()

    def load_project(self, project: Project) -> None:
        self._project = project
        for task, picker in self._pickers.items():
            picker.set_value(project.model_assignments.get(task))
            picker.set_temperature(project.task_temperatures.get(task))

    def update_available_models(self, models_dir: str) -> None:
        models = storage.list_gguf_models(models_dir)
        self._available_models = models

        if models_dir:
            self.dir_label.setText(f"Directory: {models_dir} ({len(models)} model{'s' if len(models) != 1 else ''} found)")
        else:
            self.dir_label.setText("No models directory set. Configure in Settings.")

        for picker in self._pickers.values():
            picker.update_models(models)

        # Update assign-all combo
        self.assign_all_combo.clear()
        self.assign_all_combo.addItem("— select model —", "")
        for m in models:
            self.assign_all_combo.addItem(Path(m).name, m)

    def _refresh_models(self) -> None:
        settings = storage.load_settings()
        self.update_available_models(settings.models_directory)

    def _on_model_changed(self, task: TaskType, path: str) -> None:
        if not self._project:
            return
        self._project.model_assignments.set(task, path)
        storage.save_project(self._project)
        self.assignments_changed.emit()

    def _on_temperature_changed(self, task: TaskType, value: float) -> None:
        if not self._project:
            return
        self._project.task_temperatures.set(task, value)
        storage.save_project(self._project)
        self.assignments_changed.emit()

    def _assign_to_all(self) -> None:
        path = self.assign_all_combo.currentData() or ""
        if not path:
            return
        for task, picker in self._pickers.items():
            picker.set_value(path)
            if self._project:
                self._project.model_assignments.set(task, path)
        if self._project:
            storage.save_project(self._project)
        self.assignments_changed.emit()


class SettingsPanel(QWidget):
    """
    Application settings panel (separate from per-project model assignments).
    """

    settings_changed = Signal(AppSettings)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(16)

        title = QLabel("Settings")
        title.setObjectName("heading")
        outer.addWidget(title)

        self.app_settings_widget = AppSettingsWidget()
        self.app_settings_widget.settings_changed.connect(self.settings_changed)
        outer.addWidget(self.app_settings_widget)

        # llama-cpp-python install notice
        notice_box = QGroupBox("Installation")
        nb_layout = QVBoxLayout(notice_box)
        nb_layout.setContentsMargins(14, 18, 14, 14)

        try:
            from llama_cpp import Llama
            status = "✓ llama-cpp-python is installed and ready."
            status_color = "#3d9970"
        except ImportError:
            status = (
                "✗ llama-cpp-python is not installed.\n\n"
                "Install it with:\n"
                "  pip install llama-cpp-python\n\n"
                "For GPU support:\n"
                "  CMAKE_ARGS=\"-DLLAMA_CUBLAS=on\" pip install llama-cpp-python --upgrade"
            )
            status_color = "#c0392b"

        status_lbl = QLabel(status)
        status_lbl.setStyleSheet(f"color: {status_color}; font-size: 13px;")
        status_lbl.setWordWrap(True)
        nb_layout.addWidget(status_lbl)
        outer.addWidget(notice_box)

        outer.addStretch()

    def load(self, settings: AppSettings) -> None:
        self.app_settings_widget.load(settings)
