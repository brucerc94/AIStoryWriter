"""
Image Generation Engine.

Defines the abstract ImageEngine interface and concrete backend
implementations. Currently only StableDiffusionCppEngine is provided,
but the architecture is intentionally open: any future backend
(Flux, Qwen-Image, ComfyUI, etc.) only needs to subclass ImageEngine
and implement the three abstract methods.

Nothing in the story-writing pipeline imports from this module.

Usage pattern (mirrors engine/chat.py's get_engine() singleton):

    from engine.image_engine import get_image_engine
    engine = get_image_engine()          # returns the configured singleton
    result = engine.generate(request)   # ImageGenerationResult
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Callable, Optional

from engine.models import (
    ImageBackend,
    ImageGenerationRequest,
    ImageGenerationResult,
)

logger = logging.getLogger("image_engine")


# ──────────────────────────────────────────────────────────────────────────────
# Abstract base — every backend must implement this contract
# ──────────────────────────────────────────────────────────────────────────────

class ImageEngine(ABC):
    """
    Abstract interface for image generation backends.

    Concrete subclasses:
        StableDiffusionCppEngine  — stable-diffusion.cpp via its Python bindings
        (future) FluxEngine
        (future) QwenImageEngine

    The ImageWorkflow (engine/image_workflow.py) only ever talks to this
    interface, never to a concrete implementation directly, so swapping
    backends requires zero changes to the workflow or the UI.
    """

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Human-readable backend identifier shown in the UI."""

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """
        True when the required native library / Python binding is installed.
        The UI shows a warning (not a crash) when this is False.
        """

    @property
    @abstractmethod
    def is_model_loaded(self) -> bool:
        """True after a successful load_model() call."""

    @abstractmethod
    def load_model(
        self,
        model_path: str,
        *,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Load (or hot-swap) the diffusion model at model_path.
        Raises RuntimeError on failure.
        """

    @abstractmethod
    def generate(
        self,
        request: ImageGenerationRequest,
        *,
        output_path: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> ImageGenerationResult:
        """
        Run image generation and write the result to output_path.

        progress_callback(current_step, total_steps) — called each sampling step.
        cancel_check() → True means stop as soon as possible.

        Always returns an ImageGenerationResult (never raises) so callers
        can handle errors uniformly without try/except at every call site.
        """

    @abstractmethod
    def unload_model(self) -> None:
        """Release the loaded model and free memory."""


# ──────────────────────────────────────────────────────────────────────────────
# stable-diffusion.cpp backend
# ──────────────────────────────────────────────────────────────────────────────

class StableDiffusionCppEngine(ImageEngine):
    """
    Image generation backend powered by stable-diffusion.cpp via the
    stable-diffusion-cpp-python package (pip install stable-diffusion-cpp-python).

    The package exposes:
        from stable_diffusion_cpp import StableDiffusion

    Constructor key parameters used here:
        model_path  – path to a .safetensors or .gguf SD1.x/SDXL/SD3 checkpoint
        wtype       – weight type string, e.g. "default", "q8_0", "f16"
                      "default" auto-detects from the file.
        n_threads   – CPU threads (-1 = auto / all physical cores)
        vae_decode_only – True when not doing img2img (saves memory)

    generate_image() key parameters used here:
        prompt, negative_prompt, width, height, cfg_scale,
        sample_steps, seed, progress_callback

    progress_callback signature expected by the library:
        def cb(step: int, steps: int, time: float) -> None

    Return value:
        list[PIL.Image.Image]  – output[0] is the generated image.

    The native library is loaded lazily on the first load_model() call so
    the application starts even when the library isn't installed — only
    the Images tab shows a "not installed" notice.
    """

    def __init__(self) -> None:
        self._model_path: str = ""
        # Holds the StableDiffusion high-level object after load_model().
        self._sd: Optional[object] = None

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def backend_name(self) -> str:
        return "stable-diffusion.cpp"

    # ── Availability ──────────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        """
        True when stable-diffusion-cpp-python is importable.

        The correct top-level package name is ``stable_diffusion_cpp``
        (underscore, not hyphen, and with the ``_cpp`` suffix).
        """
        try:
            import stable_diffusion_cpp  # noqa: F401
            return True
        except ImportError:
            return False

    @property
    def is_model_loaded(self) -> bool:
        return self._sd is not None

    # ── Model loading ─────────────────────────────────────────────────────────

    def load_model(
        self,
        model_path: str,
        *,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Load a Stable Diffusion checkpoint (safetensors or GGUF) using the
        stable-diffusion-cpp-python high-level API.

        Raises RuntimeError if:
          - model_path does not point to an existing file.
          - the stable-diffusion-cpp-python library is not installed.
          - the underlying C library fails to load the model.
        """
        if not model_path or not os.path.isfile(model_path):
            raise RuntimeError(f"Model file not found: {model_path!r}")

        if not self.is_available:
            raise RuntimeError(
                "stable-diffusion-cpp-python is not installed.\n"
                "Install it with:  pip install stable-diffusion-cpp-python"
            )

        # Unload any previously loaded model first.
        if self._sd is not None:
            self.unload_model()

        if progress_callback:
            progress_callback(
                f"Loading image model: {os.path.basename(model_path)}…"
            )

        logger.info(f"[sd_cpp] Loading model: {model_path}")

        try:
            from stable_diffusion_cpp import StableDiffusion  # type: ignore

            # ``wtype="default"`` lets the library auto-detect the quantisation
            # level from the file header (correct for both .safetensors and
            # GGUF files).
            # ``n_threads=-1`` delegates thread count to the C runtime, which
            # uses the number of physical CPU cores.
            # ``vae_decode_only=True`` saves memory when doing txt2img only.
            self._sd = StableDiffusion(
                model_path=model_path,
                wtype="default",
                n_threads=-1,
                vae_decode_only=True,
            )

        except Exception as exc:
            self._sd = None
            self._model_path = ""
            raise RuntimeError(
                f"stable-diffusion.cpp failed to load model {model_path!r}: {exc}"
            ) from exc

        self._model_path = model_path
        logger.info(
            f"[sd_cpp] Model loaded successfully: {os.path.basename(model_path)}"
        )

        if progress_callback:
            progress_callback("Image model ready.")

    # ── Generation ────────────────────────────────────────────────────────────

    def generate(
        self,
        request: ImageGenerationRequest,
        *,
        output_path: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> ImageGenerationResult:
        """
        Generate an image via stable-diffusion.cpp and save it to output_path.

        Uses the high-level ``StableDiffusion.generate_image()`` method:

            output = self._sd.generate_image(
                prompt           = request.prompt,
                negative_prompt  = request.negative_prompt,
                width            = request.width,
                height           = request.height,
                cfg_scale        = request.cfg_scale,
                sample_steps     = request.steps,
                seed             = request.seed,      # -1 = random
                progress_callback= <adapter>,
            )
            # output is list[PIL.Image.Image]; output[0] is the generated image.
            output[0].save(output_path)

        The library's progress_callback signature is:
            def cb(step: int, steps: int, time: float) -> None

        We adapt our (step, total_steps) callback to that signature.

        On success  → ImageGenerationResult(success=True, image_path=..., seed_used=...)
        On any error → ImageGenerationResult(success=False, error_message=...)
        Never raises.
        """
        if not self.is_model_loaded:
            return ImageGenerationResult(
                success=False,
                error_message=(
                    "No image model loaded. "
                    "Select a model in Settings → Image Model."
                ),
            )

        logger.info(
            f"[sd_cpp] generate() — task={request.task_type.value}, "
            f"prompt={request.prompt[:60]!r}, "
            f"size={request.width}×{request.height}, "
            f"steps={request.steps}, cfg={request.cfg_scale}, "
            f"seed={request.seed}"
        )

        # Build the progress adapter: the library gives us
        # (step, total_steps, elapsed_seconds) but our caller only wants
        # (current_step, total_steps).
        def _progress_adapter(step: int, steps: int, _time: float) -> None:
            if cancel_check and cancel_check():
                # stable-diffusion-cpp-python does not yet support mid-run
                # cancellation through the callback, but we log it and the
                # result will be discarded by the caller.
                logger.info("[sd_cpp] Cancel requested (will finish current step).")
            if progress_callback:
                progress_callback(step, steps)

        try:
            # Ensure the output directory exists.
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)

            # Call the library.
            output_images = self._sd.generate_image(  # type: ignore[union-attr]
                prompt=request.prompt,
                negative_prompt=request.negative_prompt,
                width=request.width,
                height=request.height,
                cfg_scale=request.cfg_scale,
                sample_steps=request.steps,
                seed=request.seed,
                progress_callback=_progress_adapter,
            )

            if not output_images:
                return ImageGenerationResult(
                    success=False,
                    error_message=(
                        "stable-diffusion.cpp returned an empty image list. "
                        "Check that the model file is valid."
                    ),
                )

            # output_images[0] is a PIL.Image.Image with an `.info` dict that
            # may contain the actual seed used.
            pil_image = output_images[0]
            actual_seed: int = request.seed
            if hasattr(pil_image, "info") and isinstance(pil_image.info, dict):
                actual_seed = int(pil_image.info.get("seed", request.seed))

            pil_image.save(output_path)
            logger.info(
                f"[sd_cpp] Image saved to {output_path!r} (seed={actual_seed})"
            )

            return ImageGenerationResult(
                success=True,
                image_path=output_path,
                seed_used=actual_seed,
            )

        except Exception as exc:
            logger.exception(f"[sd_cpp] generate() failed: {exc}")
            return ImageGenerationResult(
                success=False,
                error_message=f"Image generation failed: {exc}",
            )

    # ── Teardown ──────────────────────────────────────────────────────────────

    def unload_model(self) -> None:
        """Release the StableDiffusion object and free native memory."""
        logger.info("[sd_cpp] Unloading image model.")
        self._sd = None
        self._model_path = ""


# ──────────────────────────────────────────────────────────────────────────────
# Backend registry — maps ImageBackend enum values to engine classes
# Adding a new backend: add the class above, then add it here.
# ──────────────────────────────────────────────────────────────────────────────

_BACKEND_REGISTRY: dict[str, type[ImageEngine]] = {
    ImageBackend.STABLE_DIFFUSION_CPP.value: StableDiffusionCppEngine,
    # ImageBackend.FLUX.value: FluxEngine,
    # ImageBackend.QWEN_IMAGE.value: QwenImageEngine,
}


def create_engine_for_backend(backend: ImageBackend) -> ImageEngine:
    """
    Factory: return a new ImageEngine instance for the given backend.
    Raises ValueError if the backend isn't in the registry.
    """
    cls = _BACKEND_REGISTRY.get(backend.value)
    if cls is None:
        raise ValueError(f"Unknown image backend: {backend.value!r}")
    return cls()


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton — mirrors engine/chat.py's get_engine() pattern
# ──────────────────────────────────────────────────────────────────────────────

_image_engine_instance: Optional[ImageEngine] = None


def get_image_engine(backend: Optional[ImageBackend] = None) -> ImageEngine:
    """
    Return the module-level ImageEngine singleton, creating it on first call.

    Pass ``backend`` to switch engines at runtime (e.g. when the user
    changes the backend in Settings). The old engine is unloaded first.
    """
    global _image_engine_instance

    desired_backend = backend or ImageBackend.STABLE_DIFFUSION_CPP

    if _image_engine_instance is None:
        _image_engine_instance = create_engine_for_backend(desired_backend)
        logger.info(
            f"[image_engine] Created engine: {_image_engine_instance.backend_name}"
        )
        return _image_engine_instance

    # Hot-swap if the backend changed.
    if (
        backend is not None
        and _image_engine_instance.backend_name
        != create_engine_for_backend(backend).backend_name
    ):
        logger.info(
            f"[image_engine] Switching backend: "
            f"{_image_engine_instance.backend_name} → {backend.value}"
        )
        _image_engine_instance.unload_model()
        _image_engine_instance = create_engine_for_backend(backend)

    return _image_engine_instance