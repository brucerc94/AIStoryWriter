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

Character Portraits are handled inside the Characters tab (ui/story.py).
This panel covers Book Cover, Scene Illustration, Location, and Object/Item.
All generated images are saved encrypted inside the active project folder.

The story workflow (Synopsis, Outline, Chapters, etc.) is NOT modified
by this module in any way.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from engine import storage
from engine.character_images import PortraitOutcome, mark_generating, record_outcome
from engine.image_engine import get_image_model_profile
from engine.image_workflow import ImageWorkflowThread
from engine.models import (
    AppSettings,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageTaskType,
    Project,
)
from ui.workers import CharacterPortraitWorker, is_running, start_worker
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
        self._progress.setRange(0, 0)
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


class _ClickableImageLabel(QLabel):
    """QLabel that emits clicked when the user presses it."""
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _ImageTaskSection(QGroupBox):
    """
    A single section for one image task type (e.g. Book Cover).
    Contains a prompt/options form and a Generate button.
    After generation the result is saved encrypted into the active project.

    generate_requested(ImageGenerationRequest) is emitted when the user
    clicks Generate — the parent ImagesPanel handles the actual workflow.
    """

    generate_requested = Signal(object)


    _TASK_META: dict[ImageTaskType, dict] = {
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
        self._current_pixmap: Optional[QPixmap] = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(10)


        prompt_lbl = QLabel("Prompt")
        prompt_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        layout.addWidget(prompt_lbl)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(self._meta.get("prompt_placeholder", "Describe what to generate…"))
        self.prompt_edit.setFixedHeight(72)
        layout.addWidget(self.prompt_edit)


        neg_lbl = QLabel("Negative Prompt")
        neg_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        layout.addWidget(neg_lbl)

        self.negative_prompt_edit = QLineEdit()
        self.negative_prompt_edit.setPlaceholderText("e.g. blurry, low quality, bad anatomy, watermark…")
        layout.addWidget(self.negative_prompt_edit)


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

        options_row.addStretch()
        layout.addLayout(options_row)


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


        self._preview_lbl = _ClickableImageLabel()
        self._preview_lbl.setFixedSize(128, 128)
        self._preview_lbl.setAlignment(Qt.AlignCenter)
        self._preview_lbl.setStyleSheet(
            f"border: 1px dashed {COLOR_BORDER}; border-radius: 6px; "
            f"background: {COLOR_SURFACE}; font-size: 28px;"
        )
        self._preview_lbl.setText("🖼️")
        self._preview_lbl.setVisible(False)
        self._preview_lbl.clicked.connect(self._show_full_preview)
        layout.addWidget(self._preview_lbl, 0, Qt.AlignLeft)



    def apply_settings(self, settings: AppSettings) -> None:
        """Apply global image settings plus the selected model family's defaults."""
        if self.width_spin:
            self.width_spin.setValue(getattr(settings, "image_default_width", 512))
        if self.height_spin:
            self.height_spin.setValue(getattr(settings, "image_default_height", 512))

        profile = get_image_model_profile(getattr(settings, "image_model_path", ""))
        steps = profile.default_steps
        cfg = profile.default_cfg_scale

        if steps is None:
            steps = getattr(settings, "image_default_steps", 20)
        if cfg is None:
            cfg = getattr(settings, "image_default_cfg_scale", 7.0)

        self.steps_spin.setValue(steps)
        self.cfg_spin.setValue(cfg)

    def set_busy(self, busy: bool) -> None:
        self.generate_btn.setEnabled(not busy)
        self.generate_btn.setText("Generating…" if busy else "Generate")

    def show_status(self, text: str, icon: str = "⏳") -> None:
        self._status_bar.show_status(text, icon)

    def show_progress(self, step: int, total: int) -> None:
        self._status_bar.show_progress(step, total)

    def hide_status(self) -> None:
        self._status_bar.hide_status()

    def show_result(self, result: ImageGenerationResult, image_bytes: Optional[bytes] = None) -> None:
        if result.success:
            self._result_label.setText("✓  Saved to project (encrypted)")
            self._result_label.setStyleSheet(f"color: {COLOR_SUCCESS}; font-size: 12px;")
            if image_bytes:
                pixmap = QPixmap()
                pixmap.loadFromData(image_bytes)
                if not pixmap.isNull():
                    self._current_pixmap = pixmap
                    self._preview_lbl.setPixmap(
                        pixmap.scaled(128, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    )
                    self._preview_lbl.setStyleSheet(
                        f"border: 1px solid {COLOR_ACCENT}; border-radius: 6px; "
                        f"background: {COLOR_SURFACE};"
                    )
                    self._preview_lbl.setCursor(Qt.PointingHandCursor)
                    self._preview_lbl.setToolTip("Click to view full image")
                    self._preview_lbl.setVisible(True)
        else:
            short = (result.error_message or "Unknown error").split("\n")[0]
            self._result_label.setText(f"✗  {short}")
            self._result_label.setStyleSheet(f"color: {COLOR_ERROR}; font-size: 12px;")



    def _on_generate_clicked(self) -> None:
        request = ImageGenerationRequest(
            task_type=self.task_type,
            prompt=self.prompt_edit.toPlainText().strip(),
            negative_prompt=self.negative_prompt_edit.text().strip(),
            seed=self.seed_spin.value() if self.seed_spin else -1,
            width=self.width_spin.value() if self.width_spin else 512,
            height=self.height_spin.value() if self.height_spin else 512,
            steps=self.steps_spin.value(),
            cfg_scale=self.cfg_spin.value(),
        )
        self.generate_requested.emit(request)

    def _show_full_preview(self) -> None:
        if not hasattr(self, "_current_pixmap") or self._current_pixmap is None:
            return
        meta = self._TASK_META.get(self.task_type, {})
        title = meta.get("title", "Image Preview")
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setModal(True)
        vbox = QVBoxLayout(dialog)
        vbox.setContentsMargins(16, 16, 16, 16)
        vbox.setSpacing(12)
        img_lbl = QLabel()
        img_lbl.setAlignment(Qt.AlignCenter)
        scaled = self._current_pixmap.scaled(512, 512, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        img_lbl.setPixmap(scaled)
        vbox.addWidget(img_lbl)
        close_btn = QPushButton("Close")
        close_btn.setObjectName("accent")
        close_btn.clicked.connect(dialog.accept)
        vbox.addWidget(close_btn, 0, Qt.AlignCenter)
        dialog.exec()






class ImagesPanel(QWidget):
    """
    The Images tab.  Acts as the ImageController:
        - Owns _ImageTaskSection widgets (no Character Portrait — that lives in Characters tab)
        - "Create Character Images" button generates portraits for all project characters
          that don't have one yet, saves them encrypted inside the project folder
        - Other task images (book cover, scene, location, object) are also saved
          encrypted inside the active project folder
        - Manages ImageWorkflowThread lifecycle
        - Reads config from AppSettings (never hardcoded paths)

    No story-workflow code is touched.
    """

    open_settings_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._settings: Optional[AppSettings] = None
        self._project: Optional[Project] = None
        self._active_thread: Optional[ImageWorkflowThread] = None
        self._active_section: Optional[_ImageTaskSection] = None
        self._active_request: Optional[ImageGenerationRequest] = None
        self._sections: dict[ImageTaskType, _ImageTaskSection] = {}


        self._char_batch_thread: Optional[QThread] = None
        self._char_batch_worker: Optional[CharacterPortraitWorker] = None
        self._char_batch_project_id: str = ""
        self._active_project_id: str = ""
        self._char_batch_queue: list[str] = []
        self._char_batch_label: Optional[QLabel] = None

        self._build_ui()



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


        header_row = QHBoxLayout()

        title = QLabel("Images")
        title.setObjectName("heading")
        header_row.addWidget(title)
        header_row.addStretch()

        self._backend_badge = QLabel("")
        self._backend_badge.setObjectName("chipMuted")
        self._backend_badge.setVisible(False)
        header_row.addWidget(self._backend_badge)

        layout.addLayout(header_row)

        subtitle = QLabel(
            "Images are generated on this computer with stable-diffusion.cpp. "
            "This tab does not use the text models from the Text Models tab.\n"
            "Each image is saved encrypted inside the project it was made for."
        )
        subtitle.setObjectName("subheading")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)


        self._no_project_notice = self._build_notice(
            "📂",
            "No project open.",
            "Open a project from the sidebar to generate and save images.",
        )
        layout.addWidget(self._no_project_notice)


        self._no_model_notice = self._build_notice(
            "🖼️",
            "Image generation is not set up yet.",
            "Choose a diffusion model file (.gguf or .safetensors) in\n"
            "Settings → Image Generation. Checkpoints for SD 1.x / SDXL need only\n"
            "the diffusion model; Z-Image, Flux and Qwen Image also need a text encoder and VAE.",
        )
        open_settings_btn = QPushButton("Open Image Settings")
        open_settings_btn.setObjectName("accent")
        open_settings_btn.clicked.connect(self.open_settings_requested)
        self._no_model_notice.layout().addWidget(open_settings_btn, 0, Qt.AlignCenter)
        layout.addWidget(self._no_model_notice)

        layout.addSpacing(20)


        char_row = QHBoxLayout()
        self._character_batch_btn = QPushButton("🎭  Create Missing Character Portraits")
        self._character_batch_btn.setObjectName("accent")
        self._character_batch_btn.setMinimumHeight(36)
        self._character_batch_btn.setToolTip(
            "Generate portrait images for all project characters that don't have one yet.\n"
            "Images are saved encrypted inside the project's characters/ folder."
        )
        self._character_batch_btn.clicked.connect(self._create_character_images)
        char_row.addWidget(self._character_batch_btn)
        char_row.addStretch()
        layout.addLayout(char_row)

        self._char_batch_status = QLabel("")
        self._char_batch_status.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        self._char_batch_status.setVisible(False)
        layout.addWidget(self._char_batch_status)

        layout.addSpacing(20)


        task_order = [
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

    def _build_notice(self, icon: str, title: str, body: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("emptyStateCard")
        frame.setVisible(False)

        fl = QVBoxLayout(frame)
        fl.setContentsMargins(20, 18, 20, 18)
        fl.setSpacing(6)

        icon_lbl = QLabel(icon)
        icon_lbl.setObjectName("emptyStateIcon")
        icon_lbl.setAlignment(Qt.AlignCenter)
        fl.addWidget(icon_lbl)

        title_lbl = QLabel(f"<b>{title}</b>")
        title_lbl.setAlignment(Qt.AlignCenter)
        fl.addWidget(title_lbl)

        body_lbl = QLabel(body)
        body_lbl.setObjectName("emptyStateDesc")
        body_lbl.setAlignment(Qt.AlignCenter)
        body_lbl.setWordWrap(True)
        fl.addWidget(body_lbl)

        return frame



    def load_project(self, project: Optional[Project]) -> None:
        """Called by MainWindow whenever the active project changes."""
        self._project = project
        self._update_notices()

    def set_settings(self, settings: AppSettings) -> None:
        """Called by MainWindow whenever AppSettings change."""
        self._settings = settings

        model_path = getattr(settings, "image_model_path", "") or ""
        if model_path:
            family = get_image_model_profile(model_path).label
            self._backend_badge.setText(f"Image model: {family}")
            self._backend_badge.setToolTip(model_path)
        else:
            self._backend_badge.setText("No image model set")
            self._backend_badge.setToolTip("Set one in Settings → Image Generation.")
        self._backend_badge.setVisible(True)

        self._update_notices()

        for section in self._sections.values():
            section.apply_settings(settings)



    def _update_notices(self) -> None:
        has_project = self._project is not None
        has_model = bool(getattr(self._settings, "image_model_path", "")) if self._settings else False

        self._no_project_notice.setVisible(not has_project)
        self._no_model_notice.setVisible(has_project and not has_model)

        sections_enabled = has_project and has_model
        self._character_batch_btn.setEnabled(sections_enabled)
        for section in self._sections.values():
            section.setEnabled(sections_enabled)



    def _create_character_images(self) -> None:
        if not self._project:
            QMessageBox.warning(self, "No Project", "Open a project first.")
            return
        if is_running(self._char_batch_thread):
            return

        pending = [c.id for c in self._project.characters if not c.image_ref]
        if not pending:
            QMessageBox.information(
                self, "All Done",
                "All characters already have portraits.\n"
                "Use 'Regenerate Image' on a character card in the Story tab to update one."
            )
            return

        # The batch belongs to the project it was started on, even if the user
        # switches projects while it runs.
        self._char_batch_project_id = self._project.id
        self._char_batch_queue = pending
        self._character_batch_btn.setEnabled(False)
        self._char_batch_status.setVisible(True)
        self._start_next_char_batch()

    def _project_for_batch(self, project_id: str) -> Optional[Project]:
        if self._project and self._project.id == project_id:
            return self._project
        return storage.load_project(project_id)

    def _start_next_char_batch(self) -> None:
        project = self._project_for_batch(self._char_batch_project_id)
        char = None
        while self._char_batch_queue and project is not None and char is None:
            char_id = self._char_batch_queue.pop(0)
            char = next((c for c in project.characters if c.id == char_id), None)

        if project is None or char is None:
            self._finish_char_batch()
            return

        remaining = len(self._char_batch_queue)
        self._char_batch_status.setText(
            f"Generating portrait for {char.name}…"
            + (f" ({remaining} more after this)" if remaining else "")
        )
        mark_generating(project, char.id)

        self._char_batch_worker = CharacterPortraitWorker(project.id, char, storage.load_settings())
        self._char_batch_thread = start_worker(
            self, self._char_batch_worker, self._on_char_batch_finished
        )

    def _on_char_batch_finished(self, outcome: PortraitOutcome) -> None:
        record_outcome(outcome, loaded=self._project)
        self._char_batch_worker = None
        self._char_batch_thread = None
        self._start_next_char_batch()

    def _finish_char_batch(self) -> None:
        self._character_batch_btn.setEnabled(True)
        self._char_batch_status.setText("✓ Character portraits finished.")
        self._char_batch_project_id = ""
        self._char_batch_worker = None
        self._char_batch_thread = None



    def _on_generate_requested(self, request: ImageGenerationRequest) -> None:
        if not self._project:
            QMessageBox.warning(self, "No Project", "Open a project first.")
            return
        if self._active_thread and self._active_thread.isRunning():
            logger.warning("[images] Generation already in progress — ignoring new request.")
            return

        section = self._sections.get(request.task_type)
        if not section:
            return

        if not request.prompt:
            section.show_result(_error_result("Please enter a prompt before generating."))
            return

        self._active_section = section
        self._active_request = request
        # Remember the owner now: the user may switch projects before the job ends.
        self._active_project_id = self._project.id
        section.set_busy(True)
        section.show_status("Starting…")


        import tempfile
        output_dir = tempfile.mkdtemp(prefix="aiss_img_")

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
        if not self._active_section or not self._active_request:
            return

        image_bytes: Optional[bytes] = None

        if result.success and result.image_path and self._active_project_id:

            try:
                with open(result.image_path, "rb") as fh:
                    image_bytes = fh.read()

                import uuid, datetime as _dt
                ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                uid = uuid.uuid4().hex[:6]
                filename = f"{self._active_request.task_type.value}_{ts}_{uid}.png"
                storage.save_binary_resource(
                    self._active_project_id,
                    filename,
                    image_bytes,
                    mime_type="image/png",
                    subfolder="images",
                )

                try:
                    os.remove(result.image_path)
                    os.rmdir(os.path.dirname(result.image_path))
                except Exception:
                    pass
            except Exception as exc:
                logger.error(f"[images] Failed to save image to project: {exc}")

        self._active_section.show_result(result, image_bytes)

    def _on_error_occurred(self, message: str) -> None:
        logger.error(f"[images] Error: {message}")
        if self._active_section:
            self._active_section.show_result(_error_result(message.split("\n")[0]))

    def _on_thread_finished(self) -> None:
        if self._active_section:
            self._active_section.set_busy(False)
            self._active_section.hide_status()
        self._active_thread = None
        self._active_section = None
        self._active_request = None
        self._active_project_id = ""




def _error_result(message: str) -> ImageGenerationResult:
    return ImageGenerationResult(success=False, error_message=message)
