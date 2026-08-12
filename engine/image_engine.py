"""
Image Generation Engine.

Defines the abstract ImageEngine interface and the concrete
StableDiffusionCppEngine backend, now fully wired to the
stable-diffusion-cpp-python package (stable_diffusion_cpp).

Architecture
------------
The engine is intentionally generic:
  • Monolithic checkpoints (SD 1.x, SDXL, …): set only image_model_path.
  • Multi-component architectures (Z-Image-Turbo, Flux, Anima …):
    set diffusion_model_path (= image_model_path in AppSettings),
    plus the optional llm_path and vae_path that the model requires.

Z-Image-Turbo specifics
-----------------------
Z-Image-Turbo is a three-component model:
  diffusion_model_path  → z_image_turbo-Q4_0.gguf
  llm_path              → Qwen3-4B-ZImage-Heretic-Genesis-Q8.gguf
  vae_path              → ae.safetensors

It must be constructed with diffusion_model_path (NOT model_path) or
the C library tries to auto-detect the SD version from a GGUF that is
not a full SD checkpoint — hence the "get sd version from file failed"
and the subsequent NULL pointer error.

The engine detects a multi-component setup automatically:
  • If image_text_encoder_path OR image_vae_path is set AND
    image_model_path looks like a diffusion-only file (not a
    full checkpoint) → use diffusion_model_path + optional components.
  • Otherwise fall back to plain model_path for legacy checkpoints.

IMPORTANT — why "load tensors from model loader failed" did NOT raise
-----------------------------------------------------------------------
stable_diffusion_cpp's `StableDiffusion.__init__` only raises when the
underlying C call `new_sd_ctx()` returns a NULL pointer. In practice,
new_sd_ctx() can return a *non-NULL* context even when individual
component files are missing tensors the loader expected — it just logs
warnings/errors ("<tensor name> not in model file",
"load tensors from model loader failed") through stable_diffusion.cpp's
native logger and keeps going with a partially-initialized graph. The
crash only happens later, inside generate_image()/sample(), when the
graph tries to actually use one of those never-loaded tensors →
"ValueError: NULL pointer access".

So `self._sd is not None` after construction is NOT sufficient evidence
that the model loaded correctly. To catch this we now install a native
log callback (stable_diffusion_cpp.sd_set_log_callback) and inspect
everything the C library printed during the load call for known
failure markers before declaring success.

Root cause of the missing tensors (original hypothesis — corrected)
---------------------------------------------------------------------
An earlier version of this module guessed that "text_encoders.llm.model.*
not in model file" meant llm_path was a plain llama.cpp GGUF (tensor
names like "token_embd.*", "blk.N.*") that needed to be replaced with a
GGUF using stable-diffusion.cpp's internal naming already baked in. That
guess was WRONG and was verified against stable-diffusion.cpp's actual
source (github.com/leejet/stable-diffusion.cpp, src/name_conversion.cpp,
`llm_name_map`): plain llama.cpp GGUF naming is exactly what
stable-diffusion.cpp expects for llm_path. It loads the file, prefixes
every tensor with "text_encoders.llm." (src/stable-diffusion.cpp,
`model_loader.init_from_file(sd_ctx_params->llm_path, "text_encoders.llm.")`),
then later remaps that naming internally: "token_embd." ->
"model.embed_tokens.", "blk." -> "model.layers.", "output_norm." ->
"model.norm.", etc. So a standard llama.cpp-quantized Qwen GGUF is the
*correct* file for llm_path — the earlier static rejection of such files
was a false positive and has been removed. If "not in model file" shows
up again, treat it as a genuine mismatch (wrong file entirely, corrupted
download, wrong Qwen variant/size for this stable-diffusion.cpp build,
etc.) and inspect the native log tail surfaced by the RuntimeError below
rather than re-guessing tensor-name conventions.

Usage (mirrors engine/chat.py's get_engine() singleton):

    from engine.image_engine import get_image_engine
    engine = get_image_engine()
    result  = engine.generate(request, output_path=...)
"""

from __future__ import annotations

import ctypes
import inspect
import logging
import os
import struct
import threading
from abc import ABC, abstractmethod
from typing import Callable, Optional

from engine.models import (
    AppSettings,
    ImageBackend,
    ImageGenerationRequest,
    ImageGenerationResult,
)

logger = logging.getLogger("image_engine")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _is_diffusion_only_file(path: str) -> bool:
    """
    Heuristic: if the file name contains typical diffusion-only keywords
    (turbo, flux, diffusion_model, …) or if companion files (llm, vae)
    are provided, treat it as a standalone diffusion model rather than a
    monolithic checkpoint.

    This is deliberately lenient — the caller can always override by setting
    (or not setting) the text-encoder / VAE paths.
    """
    name = os.path.basename(path).lower()
    keywords = (
        "turbo", "diffusion_model", "flux", "anima", "ovis",
        "z_image", "zimage", "wan", "chroma", "klein",
    )
    return any(kw in name for kw in keywords)


def _sd_supports_diffusion_flash_attn() -> bool:
    """
    Check whether the installed stable_diffusion_cpp.StableDiffusion
    __init__ accepts the diffusion_flash_attn kwarg.
    """
    try:
        from stable_diffusion_cpp import StableDiffusion
        sig = inspect.signature(StableDiffusion.__init__)
        return "diffusion_flash_attn" in sig.parameters
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Minimal GGUF tensor-name reader (no external deps) — used only as a
# best-effort pre-flight sanity check, never as the primary source of
# truth. Any parsing failure is swallowed and simply skips the check.
# ──────────────────────────────────────────────────────────────────────────────

_GGUF_SIMPLE_VALUE_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def _read_gguf_tensor_names(path: str, max_tensors: int = 4000) -> Optional[set[str]]:
    """
    Return the set of tensor names stored in a GGUF file, or None if the
    file is not a readable GGUF (wrong magic, truncated, safetensors, …).

    Kept for diagnostics/logging only — see the note in
    _check_llm_gguf_matches_sdcpp_naming() below for why this is NOT used
    to reject files anymore.
    """
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            version = struct.unpack("<I", f.read(4))[0]
            if version >= 2:
                tensor_count = struct.unpack("<Q", f.read(8))[0]
                kv_count = struct.unpack("<Q", f.read(8))[0]
            else:
                tensor_count = struct.unpack("<I", f.read(4))[0]
                kv_count = struct.unpack("<I", f.read(4))[0]

            def read_str() -> str:
                (length,) = struct.unpack("<Q", f.read(8))
                return f.read(length).decode("utf-8", errors="replace")

            def skip_value(vtype: int) -> None:
                if vtype in _GGUF_SIMPLE_VALUE_SIZES:
                    f.read(_GGUF_SIMPLE_VALUE_SIZES[vtype])
                elif vtype == 8:  # string
                    read_str()
                elif vtype == 9:  # array
                    (arr_type,) = struct.unpack("<I", f.read(4))
                    (arr_len,) = struct.unpack("<Q", f.read(8))
                    for _ in range(arr_len):
                        skip_value(arr_type)
                else:
                    raise ValueError(f"unknown gguf value type {vtype}")

            for _ in range(kv_count):
                read_str()  # key
                (vtype,) = struct.unpack("<I", f.read(4))
                skip_value(vtype)

            names: set[str] = set()
            for _ in range(min(tensor_count, max_tensors)):
                name = read_str()
                (n_dims,) = struct.unpack("<I", f.read(4))
                f.read(8 * n_dims)  # dims (uint64 each)
                f.read(4)           # ggml tensor type
                f.read(8)           # offset
                names.add(name)
            return names
    except Exception:
        # Best-effort only — never let a parsing quirk break model loading.
        return None


def _check_llm_gguf_matches_sdcpp_naming(llm_path: str) -> Optional[str]:
    """
    NOTE — this used to reject files whose tensor names looked like plain
    llama.cpp GGUF naming ("token_embd.*", "blk.N.*"). That check was
    WRONG and has been removed: verified against stable-diffusion.cpp's
    actual source (src/name_conversion.cpp, `llm_name_map`), that is
    exactly the naming convention it expects for llm_path — it internally
    remaps "token_embd." -> "model.embed_tokens.", "blk." -> "model.layers.",
    "output_norm." -> "model.norm.", etc. before building the graph. A
    standard llama.cpp-quantized Qwen GGUF is the CORRECT file for
    llm_path, not a mismatch.

    We no longer do static tensor-name validation here — it requires
    replicating stable-diffusion.cpp's full (and version-dependent) name
    conversion table to avoid false positives, which isn't worth the
    risk of blocking valid files again. Real load failures are instead
    caught at runtime via the native log capture in load_model() below,
    which reflects what stable-diffusion.cpp itself actually did with
    the file rather than a static guess about its format.
    """
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Native log capture — lets us detect load failures that the Python
# binding itself doesn't turn into an exception (see module docstring).
# ──────────────────────────────────────────────────────────────────────────────

_LOAD_FAILURE_MARKERS = (
    "not in model file",
    "load tensors from model loader failed",
    "get sd version from file failed",
    "failed to load model",
    "init from file failed",
    "error loading model",
    "load tensors failed",
)

_log_lock = threading.Lock()
_log_buffer: list[str] = []
_log_callback_installed = False
_log_callback_ref = None  # kept alive so ctypes doesn't GC the CFUNCTYPE


def _install_capturing_log_callback() -> bool:
    """
    Replace stable_diffusion_cpp's default log callback with one that
    also records every native log line into `_log_buffer`, so
    load_model() can inspect what the C library actually said instead
    of trusting a non-NULL pointer alone.

    Idempotent — safe to call on every load_model() invocation.
    """
    global _log_callback_installed, _log_callback_ref
    if _log_callback_installed:
        return True
    try:
        import stable_diffusion_cpp as _sdcpp
    except ImportError:
        return False

    @_sdcpp.sd_log_callback
    def _capturing_cb(level, text, data):  # noqa: ANN001 - ctypes callback signature
        try:
            msg = text.decode("utf-8", errors="replace")
        except Exception:
            msg = str(text)
        with _log_lock:
            _log_buffer.append(msg)
        logger.debug("[sd_cpp:native] %s", msg.rstrip())

    try:
        _sdcpp.sd_set_log_callback(_capturing_cb, ctypes.c_void_p(0))
    except Exception:
        return False

    _log_callback_ref = _capturing_cb  # prevent garbage collection
    _log_callback_installed = True
    return True


def _drain_log_buffer() -> str:
    with _log_lock:
        text = "".join(_log_buffer)
        _log_buffer.clear()
    return text


def _detect_load_failure(captured_log: str) -> Optional[str]:
    """Return the first known failure marker found in captured_log, or None."""
    lowered = captured_log.lower()
    for marker in _LOAD_FAILURE_MARKERS:
        if marker in lowered:
            return marker
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Abstract base — every backend must implement this contract
# ──────────────────────────────────────────────────────────────────────────────

class ImageEngine(ABC):
    """
    Abstract interface for image generation backends.

    Concrete subclasses:
        StableDiffusionCppEngine  — stable-diffusion.cpp via its Python bindings
        (future) FluxEngine
        (future) ComfyUIEngine

    The ImageWorkflow (engine/image_workflow.py) only ever talks to this
    interface, never to a concrete implementation directly.
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
        text_encoder_path: str = "",
        vae_path: str = "",
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Load (or hot-swap) the image model.

        Parameters
        ----------
        model_path:
            Path to the primary model file.
            • Monolithic checkpoints: full SD/SDXL .safetensors or .gguf.
            • Multi-component (Z-Image, Flux, …): the diffusion-model GGUF.
        text_encoder_path:
            Optional path to a standalone LLM / text-encoder used by
            multi-component architectures.  Leave empty for monolithic
            checkpoints or when not needed by the specific model.
        vae_path:
            Optional path to a standalone VAE.  Leave empty when the VAE
            is baked into the main checkpoint.
        progress_callback:
            Called with a status string during loading.

        Raises RuntimeError on failure — including the case where the
        native library accepted the files but failed to find one or
        more expected tensors in them (see module docstring).
        """

    def load_loras(self, loras: list) -> None:
        """
        Register active LoRA adapters. Base implementation is a no-op so that
        backends that don't support LoRAs don't need to override this method.
        StableDiffusionCppEngine overrides it with a full implementation.
        """

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

        progress_callback(current_step, total_steps) — called each step.
        cancel_check() → True means stop as soon as possible.

        Always returns an ImageGenerationResult (never raises).
        """

    @abstractmethod
    def unload_model(self) -> None:
        """Release the loaded model and free memory."""


# ──────────────────────────────────────────────────────────────────────────────
# stable-diffusion.cpp backend
# ──────────────────────────────────────────────────────────────────────────────

class StableDiffusionCppEngine(ImageEngine):
    """
    Image generation backend powered by stable-diffusion.cpp
    (Python package: stable_diffusion_cpp, import as stable_diffusion_cpp).

    Supports both:
      • Monolithic checkpoints  — load via model_path=
      • Multi-component setups — load via diffusion_model_path= + llm_path= + vae_path=

    Z-Image-Turbo defaults
    ----------------------
    When a multi-component setup is detected (text_encoder_path or vae_path
    is set, or the diffusion file name matches known patterns), the engine
    automatically applies the Z-Image-Turbo recommended defaults:
        sample_steps         = 8
        cfg_scale            = 1.0
        offload_params_to_cpu = True
        diffusion_flash_attn  = True  (if supported by the installed build)

    These load-time defaults are fixed. The *generation-time* defaults
    (sample_steps / cfg_scale) are additionally enforced in generate():
    Z-Image-Turbo is a distilled/turbo model and simply does not behave
    correctly with generic SD settings (e.g. steps=20, cfg=7.0) — using
    them isn't "more thorough", it produces broken output. generate()
    therefore clamps to the recommended values whenever a multi-component
    model is loaded, unless the caller explicitly opted out via
    request.allow_custom_sampling (see generate() below).
    """

    # Z-Image-Turbo recommended generation defaults.
    _ZIMAGE_STEPS_DEFAULT: int = 8
    _ZIMAGE_CFG_DEFAULT: float = 1.0

    def __init__(self) -> None:
        self._sd = None                      # stable_diffusion_cpp.StableDiffusion instance
        self._model_path: str = ""
        self._text_encoder_path: str = ""
        self._vae_path: str = ""
        self._is_multi_component: bool = False
        self._loras: list[dict] = []
        self._loaded_lora_dir: str = ""

    # ── LoRA support ───────────────────────────────────────────────────

    def load_loras(self, loras: list) -> None:
        """Register active LoRA adapters before calling load_model()."""
        self._loras = [
            e for e in (loras or [])
            if e.get("enabled", True) and e.get("path", "").strip()
        ]

    def _lora_prompt_tags(self, base_prompt: str) -> str:
        """
        Build the effective prompt by:
          1. Prepending any trigger words required by active LoRAs
             (some models only activate with specific keywords).
          2. Appending <lora:stem:weight> tags so stable-diffusion.cpp
             loads and applies the adapter weights at sampling time.
        """
        if not self._loras:
            return base_prompt

        triggers = []
        tags = []
        for entry in self._loras:
            path = entry.get("path", "").strip()
            if not path:
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            weight = float(entry.get("weight", 0.8))
            tags.append(f"<lora:{stem}:{weight:.2f}>")

            trigger = entry.get("trigger", "").strip()
            if trigger and trigger not in base_prompt:
                triggers.append(trigger)

        if not tags:
            return base_prompt

        # Build: [triggers, ] base_prompt <lora:…> …
        parts = []
        if triggers:
            parts.append(", ".join(triggers))
        parts.append(base_prompt.strip())
        prompt = ", ".join(parts) if triggers else base_prompt.strip()
        return prompt.rstrip() + " " + " ".join(tags)

    def _lora_model_dir(self) -> str:
        """Directory of the first active LoRA, passed as lora_model_dir= to StableDiffusion."""
        if not self._loras:
            return ""
        first_path = self._loras[0].get("path", "").strip()
        return os.path.dirname(first_path) if first_path else ""

    # ── Properties ────────────────────────────────────────────────────

    @property
    def backend_name(self) -> str:
        return "stable-diffusion.cpp"

    @property
    def is_available(self) -> bool:
        """True when stable_diffusion_cpp is importable."""
        try:
            import stable_diffusion_cpp  # noqa: F401
            return True
        except ImportError:
            return False

    @property
    def is_model_loaded(self) -> bool:
        return self._sd is not None

    # ── load_model ────────────────────────────────────────────────────

    def load_model(
        self,
        model_path: str,
        *,
        text_encoder_path: str = "",
        vae_path: str = "",
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Load the image model.

        Automatically chooses between:
          • diffusion_model_path=  for multi-component setups
          • model_path=            for monolithic checkpoints

        Multi-component is assumed when any of the following is true:
          1. text_encoder_path is non-empty.
          2. vae_path is non-empty.
          3. The model filename matches known diffusion-only patterns
             AND at least one companion path is supplied.

        For Z-Image-Turbo:
            load_model(
                model_path  = "/path/z_image_turbo-Q4_0.gguf",
                text_encoder_path = "/path/Qwen3-4B-ZImage-Heretic-Genesis-Q8.gguf",
                vae_path    = "/path/ae.safetensors",
            )

        IMPORTANT: constructing `StableDiffusion(**kwargs)` succeeding
        (i.e. not raising) does NOT by itself mean the model loaded
        correctly — see the module docstring. This method additionally
        inspects the native log output captured during construction and
        treats known failure markers (e.g. "not in model file") as a
        hard failure, tearing the half-loaded context back down instead
        of leaving self._sd pointing at a broken context.
        """
        if not model_path or not os.path.isfile(model_path):
            raise RuntimeError(f"Model file not found: {model_path!r}")

        if text_encoder_path and not os.path.isfile(text_encoder_path):
            raise RuntimeError(f"Text encoder file not found: {text_encoder_path!r}")

        if vae_path and not os.path.isfile(vae_path):
            raise RuntimeError(f"VAE file not found: {vae_path!r}")

        same_model = (
            self._sd is not None
            and self._model_path == model_path
            and self._text_encoder_path == text_encoder_path
            and self._vae_path == vae_path
            and self._lora_model_dir() == getattr(self, "_loaded_lora_dir", "")
        )

        if same_model:
            logger.info("[sd_cpp] Model already loaded — reusing existing instance.")
            return


        # Unload existing model first to free memory.
        if self._sd is not None:
            self.unload_model()

        try:
            from stable_diffusion_cpp import StableDiffusion
        except ImportError as exc:
            raise RuntimeError(
                "stable_diffusion_cpp is not installed. "
                "Install stable-diffusion-cpp-python to enable image generation."
            ) from exc

        # Determine load mode.
        is_multi = bool(text_encoder_path or vae_path) or (
            _is_diffusion_only_file(model_path)
            and bool(text_encoder_path or vae_path)
        )
        # Even without companion paths, if the filename clearly signals a
        # standalone diffusion model, prefer the diffusion_model_path= path
        # so we don't trigger "get sd version from file failed".
        if not is_multi and _is_diffusion_only_file(model_path):
            is_multi = True

        # NOTE: we no longer do a static pre-flight rejection of llm_path
        # based on guessed tensor-name conventions (see
        # _check_llm_gguf_matches_sdcpp_naming's docstring for why that
        # was removed — it produced false positives on valid files).
        # The real check happens after construction, via native log
        # capture below.

        self._is_multi_component = is_multi

        # Build kwargs for StableDiffusion().
        kwargs: dict = {}

        if is_multi:
            kwargs["diffusion_model_path"] = model_path
            if text_encoder_path:
                kwargs["llm_path"] = text_encoder_path
            if vae_path:
                kwargs["vae_path"] = vae_path
            # Z-Image-Turbo / multi-component memory optimisation.
            kwargs["offload_params_to_cpu"] = True
            # Enable diffusion flash-attention if the build supports it.
            if _sd_supports_diffusion_flash_attn():
                kwargs["diffusion_flash_attn"] = True
        else:
            # Monolithic checkpoint (SD 1.x, SDXL, …).
            kwargs["model_path"] = model_path
            if vae_path:
                kwargs["vae_path"] = vae_path

        # If the caller has already registered LoRAs via load_loras(), tell
        # stable-diffusion.cpp where to find the .safetensors files so the
        # <lora:name:weight> tags injected at generation time resolve correctly.
        lora_dir = self._lora_model_dir()
        if lora_dir and os.path.isdir(lora_dir):
            kwargs["lora_model_dir"] = lora_dir
            logger.info("[sd_cpp] lora_model_dir=%s", lora_dir)

        basename = os.path.basename(model_path)
        if progress_callback:
            mode_label = "multi-component" if is_multi else "monolithic"
            progress_callback(
                f"Loading image model [{mode_label}]: {basename}…"
            )

        logger.info(
            "[sd_cpp] Loading model (mode=%s): %s | encoder=%s | vae=%s",
            "multi-component" if is_multi else "monolithic",
            model_path,
            text_encoder_path or "(none)",
            vae_path or "(none)",
        )

        # Install (once) our capturing log callback, then drain any stale
        # output so the buffer only contains what happens during THIS load.
        _install_capturing_log_callback()
        _drain_log_buffer()

        new_sd = None
        try:
            new_sd = StableDiffusion(**kwargs)
        except Exception as exc:
            captured = _drain_log_buffer()
            detail = f" Native log: {captured[-800:]}" if captured else ""
            raise RuntimeError(
                f"Failed to load image model from {model_path!r}: {exc}.{detail}"
            ) from exc

        captured_log = _drain_log_buffer()
        failure_marker = _detect_load_failure(captured_log)
        if failure_marker is not None:
            # The C library accepted the files and returned a context, but
            # logged that it could not find one or more expected tensors —
            # this WILL crash with a NULL pointer error the moment
            # generate_image() touches the missing weights. Treat it as a
            # load failure now rather than a generation-time crash later.
            try:
                # Best-effort: drop the reference so the (partially loaded,
                # broken) native context can be garbage-collected / freed.
                del new_sd
            except Exception:
                pass
            self._sd = None
            snippet = captured_log[-1200:] if captured_log else "(no output captured)"
            raise RuntimeError(
                "Image model failed to load correctly: the native library "
                f"reported '{failure_marker}'. This means diffusion_model_path, "
                "llm_path, or vae_path point at a file whose tensor names don't "
                "match what stable-diffusion.cpp expects for this architecture "
                "(see engine/image_engine.py module docstring for the exact "
                "naming stable-diffusion.cpp requires for Z-Image-Turbo's "
                "llm_path and vae_path). "
                f"Native log tail:\n{snippet}"
            )

        self._sd = new_sd
        self._model_path = model_path
        self._text_encoder_path = text_encoder_path
        self._vae_path = vae_path
        self._loaded_lora_dir = kwargs.get("lora_model_dir", "")

        logger.info("[sd_cpp] Model loaded successfully.")

    # ── generate ──────────────────────────────────────────────────────

    def generate(
        self,
        request: ImageGenerationRequest,
        *,
        output_path: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> ImageGenerationResult:
        """
        Generate an image from *request* and save it to *output_path*.

        For multi-component / Z-Image-Turbo models, request.steps and
        request.cfg_scale are clamped to the Z-Image-Turbo recommended
        values (8 steps, cfg 1.0) regardless of what the caller passed,
        because the generic SD defaults (e.g. 20 steps / cfg 7.0) are
        not just suboptimal for a turbo/distilled model — they are wrong
        and produce degraded or broken output. If request.steps or
        request.cfg_scale already match the recommended values, nothing
        changes; otherwise the override is logged so it's visible why the
        effective settings differ from what was requested.
        """
        if not self.is_model_loaded:
            return ImageGenerationResult(
                success=False,
                error_message=(
                    "No image model loaded. "
                    "Select a model in Settings → Image Generation."
                ),
            )

        effective_steps = request.steps
        effective_cfg = request.cfg_scale



        logger.info(
            "[sd_cpp] generate() — task=%s prompt=%.60r size=%dx%d steps=%d cfg=%.1f",
            request.task_type.value,
            request.prompt,
            request.width,
            request.height,
            effective_steps,
            effective_cfg,
        )

        # Build progress wrapper.
        def _progress(step: int, steps: int, _time: float) -> None:
            if progress_callback:
                progress_callback(step, steps)

        # Inject <lora:name:weight> tags for any active LoRA adapters.
        effective_prompt = self._lora_prompt_tags(request.prompt)
        if effective_prompt != request.prompt:
            logger.info(
                "[sd_cpp] LoRA tags injected — original: %.60r → effective: %.80r",
                request.prompt,
                effective_prompt,
            )

        try:
            images = self._sd.generate_image(
                prompt=effective_prompt,
                negative_prompt=request.negative_prompt or "",
                width=request.width,
                height=request.height,
                sample_steps=effective_steps,
                cfg_scale=effective_cfg,
                seed=request.seed,
                progress_callback=_progress,
            )

            if not images:
                return ImageGenerationResult(
                    success=False,
                    error_message="generate_image() returned an empty list.",
                )

            # Save the first image (generate_image returns a list).
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            images[0].save(output_path)

            # Try to read back the seed actually used.
            seed_used = request.seed
            try:
                seed_used = int(images[0].info.get("seed", request.seed))
            except Exception:
                pass

            logger.info("[sd_cpp] Image saved to %s (seed=%d)", output_path, seed_used)
            return ImageGenerationResult(
                success=True,
                image_path=output_path,
                seed_used=seed_used,
            )

        except Exception as exc:
            logger.exception("[sd_cpp] generate() failed: %s", exc)
            return ImageGenerationResult(
                success=False,
                error_message=str(exc),
            )

    # ── unload_model ──────────────────────────────────────────────────

    def unload_model(self) -> None:
        logger.info("[sd_cpp] Unloading image model.")
        self._sd = None
        self._model_path = ""
        self._text_encoder_path = ""
        self._vae_path = ""
        self._is_multi_component = False


# ──────────────────────────────────────────────────────────────────────────────
# Backend registry
# ──────────────────────────────────────────────────────────────────────────────

_BACKEND_REGISTRY: dict[str, type[ImageEngine]] = {
    ImageBackend.STABLE_DIFFUSION_CPP.value: StableDiffusionCppEngine,
    # ImageBackend.FLUX.value: FluxEngine,
}


def create_engine_for_backend(backend: ImageBackend) -> ImageEngine:
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
        logger.info("[image_engine] Created engine: %s", _image_engine_instance.backend_name)
        return _image_engine_instance

    # Hot-swap if the backend changed.
    if (
        backend is not None
        and _image_engine_instance.backend_name
        != create_engine_for_backend(backend).backend_name
    ):
        logger.info(
            "[image_engine] Switching backend: %s → %s",
            _image_engine_instance.backend_name,
            backend.value,
        )
        _image_engine_instance.unload_model()
        _image_engine_instance = create_engine_for_backend(backend)

    return _image_engine_instance


def load_image_engine_from_settings(settings: AppSettings) -> None:
    """
    Convenience function called by the UI after settings change.
    Reads image_model_path, image_text_encoder_path, and image_vae_path
    from AppSettings and (re)loads the engine.

    Safe to call even when no model is configured (returns silently).

    FAIL LOUD, NOT SILENT: a multi-component model (Z-Image-Turbo, Flux, …)
    whose diffusion file is set but whose text_encoder/vae paths come back
    empty from AppSettings must NOT reach StableDiffusion() as
    "encoder=(none) vae=(none)" — that's exactly what produces the
    "not in model file" / NULL pointer crash. If AppSettings doesn't have
    image_text_encoder_path / image_vae_path populated (missing attribute
    on AppSettings, or the Settings UI never wrote them), we raise here
    with a message that names precisely which AppSettings field is
    missing, instead of silently calling load_model() with "".
    """
    if not settings.image_model_path:
        return

    try:
        backend = ImageBackend(settings.image_backend)
    except ValueError:
        backend = ImageBackend.STABLE_DIFFUSION_CPP

    text_encoder_path = getattr(settings, "image_text_encoder_path", "") or ""
    vae_path = getattr(settings, "image_vae_path", "") or ""

    if _is_diffusion_only_file(settings.image_model_path):
        missing = []
        if not text_encoder_path:
            missing.append("image_text_encoder_path (Settings → Image Generation → Text Encoder)")
        if not vae_path:
            missing.append("image_vae_path (Settings → Image Generation → VAE)")
        if missing:
            raise RuntimeError(
                f"'{os.path.basename(settings.image_model_path)}' is a multi-component "
                "diffusion-only model (e.g. Z-Image-Turbo) and requires a Text Encoder "
                "and a VAE, but the following AppSettings field(s) are empty: "
                + "; ".join(missing) +
                ". Add these fields to AppSettings (see engine/models.py) and wire "
                "them up in the Settings dialog before load_image_engine_from_settings "
                "is called, otherwise StableDiffusion() is invoked with "
                "llm_path='' / vae_path='' and every text-encoder/VAE tensor will be "
                "reported 'not in model file'."
            )

    logger.info(
        "[image_engine] load_image_engine_from_settings: diffusion=%s | encoder=%s | vae=%s",
        settings.image_model_path,
        text_encoder_path or "(none)",
        vae_path or "(none)",
    )

    engine = get_image_engine(backend)
    engine.load_model(
        model_path=settings.image_model_path,
        text_encoder_path=text_encoder_path,
        vae_path=vae_path,
    )