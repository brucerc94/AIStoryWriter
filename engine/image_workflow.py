"""
Image Generation Workflow.

Runs image generation in a background QThread, exactly mirroring the
pattern of engine/workflow.py (WorkflowWorker / WorkflowThread).

The story-writing pipeline is completely untouched by this module.

Architecture:
    ImageController (ui/images.py)
        └─ ImageWorkflowThread (this module)
               └─ ImageWorkflowWorker (this module)
                      └─ ImageEngine (engine/image_engine.py)
                             └─ StableDiffusionCppEngine (or future backend)

NOT YET CONNECTED to the story workflow — that integration is a future
phase once the image engine itself is fully wired up.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

from engine import storage
from engine.image_engine import get_image_engine
from engine.models import (
    AppSettings,
    ImageBackend,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageTaskType,
)

logger = logging.getLogger("image_workflow")


class ImageWorkflowWorker(QObject):
    """
    Runs inside a QThread. Drives the image engine and emits signals back
    to the UI (ImagePanel in ui/images.py).

    Signal contract mirrors WorkflowWorker in engine/workflow.py so the
    UI can handle both with the same patterns.
    """

    # Emitted for each sampling step: (current_step, total_steps)
    progress_updated = Signal(int, int)

    # Status text while the engine is working
    status_changed = Signal(str)

    # Model load/unload status messages
    model_loading = Signal(str)

    # Generation complete — carries the ImageGenerationResult
    generation_finished = Signal(object)

    # Unrecoverable error
    error_occurred = Signal(str)

    # All done (success or failure)
    finished = Signal()

    def __init__(
        self,
        request: ImageGenerationRequest,
        settings: Optional[AppSettings] = None,
        output_directory: str = "",
    ) -> None:
        super().__init__()
        self.request = request
        self.settings = settings
        self.output_directory = output_directory or _default_output_directory()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        logger.info(
            f"[image_workflow] Task started: {self.request.task_type.value}"
        )
        try:
            self._run_generation()
        except Exception as e:
            import traceback
            msg = f"Image workflow error: {e}\n{traceback.format_exc()}"
            logger.error(msg)
            self.error_occurred.emit(msg)
        finally:
            self.finished.emit()

    # ──────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────

    def _run_generation(self) -> None:
        self.status_changed.emit("Loading image model…")

        if not self._model_path():
            self.error_occurred.emit(
                "No image model selected.\n\n"
                "Go to Settings and set an Image Model path."
            )
            return

        backend = self._backend()
        engine = get_image_engine(backend)
        if not engine.is_available:
            self.error_occurred.emit(
                f"The {engine.backend_name} library is not installed.\n\n"
                "Install it and restart AI Story Studio."
            )
            return

        try:
            engine.load_model(
                self._model_path(),
                text_encoder_path=self._text_encoder_path(),
                vae_path=self._vae_path(),
                progress_callback=lambda msg: self.model_loading.emit(msg),
            )
        except RuntimeError as e:
            self.error_occurred.emit(f"Failed to load image model:\n{e}")
            return

        if self._cancelled:
            return

        output_path = self._make_output_path()
        self.status_changed.emit("Generating image…")
        logger.info(
            f"[image_workflow] Generating: task={self.request.task_type.value}, "
            f"output={output_path}"
        )

        result = engine.generate(
            self.request,
            output_path=output_path,
            progress_callback=lambda step, total: self.progress_updated.emit(step, total),
            cancel_check=lambda: self._cancelled,
        )

        if self._cancelled:
            logger.info("[image_workflow] Generation cancelled by user.")
            return

        if result.success:
            logger.info(f"[image_workflow] Generation succeeded: {result.image_path}")
            self.status_changed.emit("Done.")
        else:
            logger.warning(f"[image_workflow] Generation failed: {result.error_message}")

        self.generation_finished.emit(result)

    def _model_path(self) -> str:
        if self.settings:
            return getattr(self.settings, "image_model_path", "") or ""
        return ""

    def _text_encoder_path(self) -> str:
        if self.settings:
            return getattr(self.settings, "image_text_encoder_path", "") or ""
        return ""

    def _vae_path(self) -> str:
        if self.settings:
            return getattr(self.settings, "image_vae_path", "") or ""
        return ""

    def _backend(self) -> ImageBackend:
        if self.settings:
            raw = getattr(self.settings, "image_backend", "")
            try:
                return ImageBackend(raw)
            except ValueError:
                pass
        return ImageBackend.STABLE_DIFFUSION_CPP

    def _make_output_path(self) -> str:
        return _make_output_path_from_request(self.request, self.output_directory)


def _make_output_path_from_request(request: ImageGenerationRequest, output_directory: str) -> str:
    os.makedirs(output_directory, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:6]
    filename = f"{request.task_type.value}_{timestamp}_{uid}.png"
    return os.path.join(output_directory, filename)


def _default_output_directory() -> str:
    """Fallback output directory when none is configured in Settings."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "images",
    )


def generate_character_image(project_id: str, character, settings: Optional[AppSettings] = None) -> tuple[bool, Optional[dict], str]:
    """Generate a character image and persist it through the existing encrypted storage layer."""
    prompt = (
        f"Portrait of {character.name}. "
        f"Role: {character.role}. "
        "Focus only on physical appearance, age, visible traits, and clothing. "
        f"Description: {character.description}. "
        "Do not include personality, backstory, or internal traits."
    )
    request = ImageGenerationRequest(
        task_type=ImageTaskType.CHARACTER_PORTRAIT,
        prompt=prompt,
        negative_prompt="blurry, low quality, bad anatomy, text, watermark",
        width=getattr(settings, "image_default_width", 512) if settings else 512,
        height=getattr(settings, "image_default_height", 512) if settings else 512,
        steps=getattr(settings, "image_default_steps", 20) if settings else 20,
        cfg_scale=getattr(settings, "image_default_cfg_scale", 7.0) if settings else 7.0,
    )
    # Use a temp directory for the raw engine output; the final copy goes
    # through the encrypted storage layer into the project's characters/ folder.
    import tempfile
    output_dir = tempfile.mkdtemp(prefix="aiss_img_")
    output_path = os.path.join(output_dir, f"character_{character.id}.png")
    engine = get_image_engine(_backend(settings))
    if not engine.is_available:
        return False, None, f"The {engine.backend_name} library is not installed."

    try:
        engine.load_model(
            _model_path(settings),
            text_encoder_path=_text_encoder_path(settings),
            vae_path=_vae_path(settings),
            progress_callback=lambda _msg: None,
        )
    except RuntimeError as exc:
        return False, None, str(exc)

    result = engine.generate(
        request,
        output_path=output_path,
        progress_callback=lambda _step, _total: None,
        cancel_check=lambda: False,
    )
    if not result.success or not result.image_path:
        return False, None, result.error_message or "Image generation failed."

    with open(result.image_path, "rb") as fh:
        image_bytes = fh.read()

    # Clean up the temp file used by the engine
    try:
        os.remove(result.image_path)
        os.rmdir(output_dir)
    except Exception:
        pass

    image_ref = storage.save_binary_resource(
        project_id,
        f"character_{character.id}.png",
        image_bytes,
        mime_type="image/png",
        subfolder="characters",
    )
    return True, image_ref, ""


def _backend(settings: Optional[AppSettings]) -> ImageBackend:
    if settings:
        raw = getattr(settings, "image_backend", "")
        try:
            return ImageBackend(raw)
        except ValueError:
            pass
    return ImageBackend.STABLE_DIFFUSION_CPP


def _model_path(settings: Optional[AppSettings]) -> str:
    if settings:
        return getattr(settings, "image_model_path", "") or ""
    return ""


def _text_encoder_path(settings: Optional[AppSettings]) -> str:
    if settings:
        return getattr(settings, "image_text_encoder_path", "") or ""
    return ""


def _vae_path(settings: Optional[AppSettings]) -> str:
    if settings:
        return getattr(settings, "image_vae_path", "") or ""
    return ""


# ──────────────────────────────────────────────────────────────────────────────
# Thread wrapper — mirrors WorkflowThread in engine/workflow.py
# ──────────────────────────────────────────────────────────────────────────────

class ImageWorkflowThread(QThread):
    """Owns the worker and runs it in a background thread."""

    progress_updated = Signal(int, int)
    status_changed = Signal(str)
    model_loading = Signal(str)
    generation_finished = Signal(object)
    error_occurred = Signal(str)

    def __init__(
        self,
        request: ImageGenerationRequest,
        settings: Optional[AppSettings] = None,
        output_directory: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.worker = ImageWorkflowWorker(request, settings, output_directory)
        self.worker.moveToThread(self)

        # Forward signals
        self.worker.progress_updated.connect(self.progress_updated)
        self.worker.status_changed.connect(self.status_changed)
        self.worker.model_loading.connect(self.model_loading)
        self.worker.generation_finished.connect(self.generation_finished)
        self.worker.error_occurred.connect(self.error_occurred)

    def run(self) -> None:
        self.worker.run()

    def cancel(self) -> None:
        self.worker.cancel()