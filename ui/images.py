"""
Images Panel.

A self-contained tab for AI image generation, completely independent
of the story-writing workflow. Follows the same visual and structural
conventions as ui/story.py, ui/chat.py, etc.

Architecture:
    ImagesPanel (this file) acts as the ImageController:
        ├─ Manages the UI for all image task types
        ├─ Reads image config from AppSettings (no hardcoded paths)
        └─ Delegates generation to ImageWorkflowThread

The story workflow (Synopsis, Outline, Chapters, etc.) is NOT modified
by this module in any way.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from engine.image_workflow import ImageWorkflowThread
from engine.models import (
    AppSettings,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageTaskType,
)
from ui.styles import (
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_BORDER_LIGHT,
    COLOR_SURFACE,
    COLOR_SURFACE_RAISED,
    COLOR_TEXT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    COLOR_ERROR,
    COLOR_SUCCESS,
    COLOR_WARNING,
)

logger = logging.getLogger("ui.images")


# ──────────────────────────────────────────────────────────────────────────────
# Helper widgets
# ──────────────────────────────────────────────────────────────────────────────

class _SectionDivider(QFrame):
    """Thin horizontal rule between image generation sections."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.HLine)
        self.setStyleSheet(f"color: {COLOR_BORDER}; margin: 8px 0;")


class _StatusBar(QWidget):
    """
    Small status strip shown below the generate button.
    Mirrors the AI status bar pattern from ui/chat.py.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setVisible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)

        self._icon = QLabel("⏳")
        self._icon.setStyleSheet("font-size: 14px; background: transparent;")
        layout.addWidget(self._icon)

        self._label = QLabel("")
        self._label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px; background: transparent;")
        layout.addWidget(self._label, 1)

        self._progress = QProgressBar()
        self._progress.setObjectName("aiProgress")
        self._progress.setRange(0, 0)        # indeterminate by default
        self._progress.setFixedHeight(3)
        self._progress.setFixedWidth(120)
        layout.addWidget(self._progress)

    def show_status(self, text: str, icon: str = "⏳") -> None:
        self._icon.setText(icon)
        self._label.setText(text)
        self.setVisible(True)

    def show_progress(self, step: int, total: int) -> None:
        if total > 0:
            self._progress.setRange(0, total)
            self._progress.setValue(step)
        else:
            self._progress.setRange(0, 0)
        self._label.setText(f"Step {step}/{total}" if total > 0 else "Generating…")

    def hide_status(self) -> None:
        self.setVisible(False)


class _ImageTaskSection(QGroupBox):
    """
    A single collapsible section for one image task type (e.g. Character
    Portrait). Contains a prompt/options form and a Generate button.

    generate_requested(ImageGenerationRequest) is emitted when the user
    clicks Generate — the parent ImagesPanel handles the actual workflow.
    """

    generate_requested = Signal(object)   # ImageGenerationRequest

    # ── Section definitions ──────────────────────────────────────────────
    # Each task type declares which extra fields it needs beyond the
    # universal prompt / negative-prompt pair.
    _TASK_META: dict[ImageTaskType, dict] = {
        ImageTaskType.CHARACTER_PORTRAIT: {
            "title": "Character Portrait",
            "icon": "🧑",
            "prompt_placeholder": "e.g. A stoic warrior woman with silver hair, wearing plate armor, dramatic lighting…",
            "show_seed": True,
            "show_dimensions": True,
        },
        ImageTaskType.BOOK_COVER: {
            "title": "Book Cover",
            "icon": "📖",
            "prompt_placeholder": "e.g. A fog-shrouded gothic castle at midnight, ravens circling a full moon…",
            "show_seed": True,
            "show_dimensions": True,
        },
        ImageTaskType.SCENE_ILLUSTRATION: {
            "title": "Scene Illustration",
            "icon": "🎬",
            "prompt_placeholder": "e.g. A tense confrontation in a candlelit throne room, two figures facing each other…",
            "show_seed": True,
            "show_dimensions": True,
        },
        ImageTaskType.LOCATION: {
            "title": "Location",
            "icon": "🏔️",
            "prompt_placeholder": "e.g. A vast underground cavern lit by bioluminescent fungi, ancient stone bridges…",
            "show_seed": True,
            "show_dimensions": True,
        },
        ImageTaskType.OBJECT_ITEM: {
            "title": "Object / Item",
            "icon": "🗡️",
            "prompt_placeholder": "e.g. An ornate silver dagger with a ruby-encrusted crossguard, on velvet…",
            "show_seed": True,
            "show_dimensions": False,
        },
    }

    def __init__(self, task_type: ImageTaskType, parent=None) -> None:
        meta = self._TASK_META.get(task_type, {})
        title = f"{meta.get('icon', '')}  {meta.get('title', task_type.value)}"
        super().__init__(title, parent)
        self.task_type = task_type
        self._meta = meta
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(10)

        # ── Prompt ──────────────────────────────────────────────────────
        prompt_lbl = QLabel("Prompt")
        prompt_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        layout.addWidget(prompt_lbl)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(self._meta.get("prompt_placeholder", "Describe what to generate…"))
        self.prompt_edit.setFixedHeight(72)
        layout.addWidget(self.prompt_edit)

        # ── Negative Prompt ─────────────────────────────────────────────
        neg_lbl = QLabel("Negative Prompt")
        neg_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        layout.addWidget(neg_lbl)

        self.negative_prompt_edit = QLineEdit()
        self.negative_prompt_edit.setPlaceholderText("e.g. blurry, low quality, bad anatomy, watermark…")
        layout.addWidget(self.negative_prompt_edit)

        # ── Optional fields row ──────────────────────────────────────────
        options_row = QHBoxLayout()
        options_row.setSpacing(16)

        if self._meta.get("show_seed", True):
            seed_lbl = QLabel("Seed")
            seed_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
            options_row.addWidget(seed_lbl)

            self.seed_spin = QSpinBox()
            self.seed_spin.setRange(-1, 2_147_483_647)
            self.seed_spin.setValue(-1)
            self.seed_spin.setFixedWidth(110)
            self.seed_spin.setToolTip("-1 = random seed")
            self.seed_spin.setSpecialValueText("Random")
            options_row.addWidget(self.seed_spin)
        else:
            self.seed_spin = None

        if self._meta.get("show_dimensions", True):
            w_lbl = QLabel("Width")
            w_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
            options_row.addWidget(w_lbl)

            self.width_spin = QSpinBox()
            self.width_spin.setRange(64, 2048)
            self.width_spin.setSingleStep(64)
            self.width_spin.setValue(512)
            self.width_spin.setSuffix(" px")
            self.width_spin.setFixedWidth(100)
            options_row.addWidget(self.width_spin)

            h_lbl = QLabel("Height")
            h_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
            options_row.addWidget(h_lbl)

            self.height_spin = QSpinBox()
            self.height_spin.setRange(64, 2048)
            self.height_spin.setSingleStep(64)
            self.height_spin.setValue(512)
            self.height_spin.setSuffix(" px")
            self.height_spin.setFixedWidth(100)
            options_row.addWidget(self.height_spin)
        else:
            self.width_spin = None
            self.height_spin = None

        if self._meta.get("show_sampling", True):
            steps_lbl = QLabel("Steps")
            steps_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
            options_row.addWidget(steps_lbl)

            self.steps_spin = QSpinBox()
            self.steps_spin.setRange(1, 200)
            self.steps_spin.setValue(20)
            self.steps_spin.setFixedWidth(80)
            self.steps_spin.setToolTip(
                "Number of diffusion sampling steps.\n"
                "Recommended: 8 for Z-Image-Turbo, 4 for Flux schnell, 20 for SD."
            )
            options_row.addWidget(self.steps_spin)

            cfg_lbl = QLabel("CFG")
            cfg_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
            options_row.addWidget(cfg_lbl)

            self.cfg_spin = QDoubleSpinBox()
            self.cfg_spin.setRange(1.0, 30.0)
            self.cfg_spin.setSingleStep(0.5)
            self.cfg_spin.setValue(7.0)
            self.cfg_spin.setDecimals(1)
            self.cfg_spin.setFixedWidth(80)
            self.cfg_spin.setToolTip(
                "Classifier-free guidance scale.\n"
                "Recommended: 1.0 for Z-Image-Turbo / Flux, 7.0 for SD."
            )
            options_row.addWidget(self.cfg_spin)
        else:
            self.steps_spin = None
            self.cfg_spin = None

        options_row.addStretch()
        layout.addLayout(options_row)

        # ── Status + Generate ────────────────────────────────────────────
        self._status_bar = _StatusBar()
        layout.addWidget(self._status_bar)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 4, 0, 0)

        self._result_label = QLabel("")
        self._result_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        self._result_label.setWordWrap(True)
        btn_row.addWidget(self._result_label, 1)

        self.generate_btn = QPushButton("Generate")
        self.generate_btn.setObjectName("accent")
        self.generate_btn.setMinimumHeight(34)
        self.generate_btn.setMinimumWidth(110)
        self.generate_btn.clicked.connect(self._on_generate_clicked)
        btn_row.addWidget(self.generate_btn)

        layout.addLayout(btn_row)

    # ── Public API ───────────────────────────────────────────────────────

    def apply_settings(self, settings: AppSettings) -> None:
        """Pre-fill dimensions and sampling defaults from AppSettings."""
        if self.width_spin:
            self.width_spin.setValue(getattr(settings, "image_default_width", 512))
        if self.height_spin:
            self.height_spin.setValue(getattr(settings, "image_default_height", 512))
        if self.steps_spin:
            self.steps_spin.setValue(getattr(settings, "image_default_steps", 20))
        if self.cfg_spin:
            self.cfg_spin.setValue(getattr(settings, "image_default_cfg_scale", 7.0))

    def set_busy(self, busy: bool) -> None:
        self.generate_btn.setEnabled(not busy)
        if busy:
            self.generate_btn.setText("Generating…")
        else:
            self.generate_btn.setText("Generate")

    def show_status(self, text: str, icon: str = "⏳") -> None:
        self._status_bar.show_status(text, icon)

    def show_progress(self, step: int, total: int) -> None:
        self._status_bar.show_progress(step, total)

    def hide_status(self) -> None:
        self._status_bar.hide_status()

    def show_result(self, result: ImageGenerationResult) -> None:
        if result.success:
            fname = os.path.basename(result.image_path)
            self._result_label.setText(f"✓  Saved: {fname}")
            self._result_label.setStyleSheet(f"color: {COLOR_SUCCESS}; font-size: 12px;")
        else:
            short = result.error_message.split("\n")[0]
            self._result_label.setText(f"✗  {short}")
            self._result_label.setStyleSheet(f"color: {COLOR_ERROR}; font-size: 12px;")

    # ── Internal ─────────────────────────────────────────────────────────

    def _on_generate_clicked(self) -> None:
        request = ImageGenerationRequest(
            task_type=self.task_type,
            prompt=self.prompt_edit.toPlainText().strip(),
            negative_prompt=self.negative_prompt_edit.text().strip(),
            seed=self.seed_spin.value() if self.seed_spin else -1,
            width=self.width_spin.value() if self.width_spin else 512,
            height=self.height_spin.value() if self.height_spin else 512,
            steps=self.steps_spin.value() if self.steps_spin else 20,
            cfg_scale=self.cfg_spin.value() if self.cfg_spin else 7.0,
        )
        self.generate_requested.emit(request)


# ──────────────────────────────────────────────────────────────────────────────
# Main panel
# ──────────────────────────────────────────────────────────────────────────────

class ImagesPanel(QWidget):
    """
    The Images tab.  Acts as the ImageController:
        - Owns all _ImageTaskSection widgets
        - Manages ImageWorkflowThread lifecycle
        - Reads config from AppSettings (never hardcoded paths)

    No story-workflow code is touched.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._settings: Optional[AppSettings] = None
        self._active_thread: Optional[ImageWorkflowThread] = None
        self._active_section: Optional[_ImageTaskSection] = None
        self._sections: dict[ImageTaskType, _ImageTaskSection] = {}
        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(0)

        # ── Header ───────────────────────────────────────────────────────
        header_row = QHBoxLayout()

        title = QLabel("Images")
        title.setObjectName("heading")
        header_row.addWidget(title)
        header_row.addStretch()

        # Backend badge — read-only info chip
        self._backend_badge = QLabel("")
        self._backend_badge.setObjectName("chipMuted")
        self._backend_badge.setVisible(False)
        header_row.addWidget(self._backend_badge)

        layout.addLayout(header_row)

        subtitle = QLabel(
            "Generate images for your story — characters, covers, scenes, locations, and objects."
        )
        subtitle.setObjectName("subheading")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # No-model notice (shown when image_model_path is empty)
        self._no_model_notice = self._build_no_model_notice()
        layout.addWidget(self._no_model_notice)

        layout.addSpacing(20)

        # ── Task sections ─────────────────────────────────────────────────
        task_order = [
            ImageTaskType.CHARACTER_PORTRAIT,
            ImageTaskType.BOOK_COVER,
            ImageTaskType.SCENE_ILLUSTRATION,
            ImageTaskType.LOCATION,
            ImageTaskType.OBJECT_ITEM,
        ]

        for i, task_type in enumerate(task_order):
            section = _ImageTaskSection(task_type)
            section.generate_requested.connect(self._on_generate_requested)
            self._sections[task_type] = section
            layout.addWidget(section)

            if i < len(task_order) - 1:
                layout.addSpacing(16)

        layout.addStretch()

        scroll.setWidget(inner)
        outer.addWidget(scroll)

    def _build_no_model_notice(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("emptyStateCard")
        frame.setVisible(False)

        fl = QVBoxLayout(frame)
        fl.setContentsMargins(20, 18, 20, 18)
        fl.setSpacing(6)

        icon = QLabel("🖼️")
        icon.setObjectName("emptyStateIcon")
        icon.setAlignment(Qt.AlignCenter)
        fl.addWidget(icon)

        msg = QLabel(
            "No image model configured.\n\n"
            "Go to <b>Settings</b> and set an <b>Image Model</b> path to enable generation.\n"
            "The model file should be a diffusion checkpoint compatible with the selected backend."
        )
        msg.setObjectName("emptyStateDesc")
        msg.setAlignment(Qt.AlignCenter)
        msg.setWordWrap(True)
        fl.addWidget(msg)

        return frame

    # ── Public API ───────────────────────────────────────────────────────

    def set_settings(self, settings: AppSettings) -> None:
        """
        Called by MainWindow whenever AppSettings change.
        Updates the backend badge and pre-fills dimension defaults.
        """
        self._settings = settings

        # Backend badge
        backend_raw = getattr(settings, "image_backend", "stable_diffusion_cpp")
        friendly = {
            "stable_diffusion_cpp": "stable-diffusion.cpp",
        }.get(backend_raw, backend_raw)
        self._backend_badge.setText(f"Backend: {friendly}")
        self._backend_badge.setVisible(True)

        # No-model notice
        has_model = bool(getattr(settings, "image_model_path", ""))
        self._no_model_notice.setVisible(not has_model)

        # Pre-fill dimensions in all sections
        for section in self._sections.values():
            section.apply_settings(settings)

    # ── Generation ───────────────────────────────────────────────────────

    def _on_generate_requested(self, request: ImageGenerationRequest) -> None:
        if self._active_thread and self._active_thread.isRunning():
            logger.warning("[images] Generation already in progress — ignoring new request.")
            return

        section = self._sections.get(request.task_type)
        if not section:
            return

        if not request.prompt:
            section.show_result(
                _error_result("Please enter a prompt before generating.")
            )
            return

        self._active_section = section
        section.set_busy(True)
        section.show_status("Starting…")

        output_dir = getattr(self._settings, "image_output_directory", "") if self._settings else ""

        thread = ImageWorkflowThread(
            request=request,
            settings=self._settings,
            output_directory=output_dir,
            parent=self,
        )
        thread.status_changed.connect(self._on_status_changed)
        thread.model_loading.connect(self._on_model_loading)
        thread.progress_updated.connect(self._on_progress_updated)
        thread.generation_finished.connect(self._on_generation_finished)
        thread.error_occurred.connect(self._on_error_occurred)
        thread.finished.connect(self._on_thread_finished)

        self._active_thread = thread
        thread.start()

    def _on_status_changed(self, text: str) -> None:
        if self._active_section:
            self._active_section.show_status(text)

    def _on_model_loading(self, text: str) -> None:
        if self._active_section:
            self._active_section.show_status(text, "💾")

    def _on_progress_updated(self, step: int, total: int) -> None:
        if self._active_section:
            self._active_section.show_progress(step, total)

    def _on_generation_finished(self, result: object) -> None:
        if self._active_section:
            self._active_section.show_result(result)

    def _on_error_occurred(self, message: str) -> None:
        logger.error(f"[images] Error: {message}")
        if self._active_section:
            self._active_section.show_result(
                _error_result(message.split("\n")[0])
            )

    def _on_thread_finished(self) -> None:
        if self._active_section:
            self._active_section.set_busy(False)
            self._active_section.hide_status()
        self._active_thread = None
        self._active_section = None


# ── Small helpers ────────────────────────────────────────────────────────────

def _error_result(message: str) -> ImageGenerationResult:
    from engine.models import ImageGenerationResult
    return ImageGenerationResult(success=False, error_message=message)