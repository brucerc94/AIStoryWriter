"""
LLM inference engine.
Wraps llama-cpp-python. Manages model loading and unloading.
One model loaded at a time to conserve RAM.

Also auto-detects the NVIDIA GPU (via nvidia-smi) and, for cards without
real Tensor Cores (e.g. the GTX 16-series, Compute Capability 7.5 but no
Tensor Cores unlike RTX 20-series which shares the same CC), forces
GGML_CUDA_FORCE_MMQ=1 and disables flash attention — flash attention and
the default cuBLAS/Tensor Core matmul kernels are slower or unsupported
on those GPUs.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, Iterator, Optional

_llama_available = False
try:
    from llama_cpp import Llama
    _llama_available = True
except ImportError:
    pass


logger = logging.getLogger("llm_engine")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# GPU name substrings known to lack real Tensor Cores despite a
# Tensor-Core-era Compute Capability (Turing GTX 16-series) or being
# older architectures entirely (Pascal/Maxwell GTX 9/10-series).
_NO_TENSOR_CORE_MARKERS = ("GTX 16", "GTX 10", "GTX 9", "GTX 7")


def _detect_gpu_info() -> Optional[dict]:
    """Query the first NVIDIA GPU via nvidia-smi. Returns None if unavailable."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        line = result.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            return None
        name, mem_total, mem_free, compute_cap = parts[:4]
        return {
            "name": name,
            "mem_total": int(float(mem_total)),
            "mem_free": int(float(mem_free)),
            "compute_cap": compute_cap,
        }
    except Exception:
        return None


def _needs_mmq_fallback(gpu_name: str) -> bool:
    """True for GPUs with no real Tensor Cores (needs MMQ, no flash attention)."""
    upper = gpu_name.upper()
    return any(marker in upper for marker in _NO_TENSOR_CORE_MARKERS)


class LLMEngine:
    """
    Singleton-style LLM engine.
    Loads a GGUF model on demand and unloads the previous one
    when a new model path is requested.
    """

    def __init__(self) -> None:
        self._model: Optional[object] = None  # Llama instance
        self._current_path: str = ""
        self._lock = threading.Lock()

    @property
    def is_available(self) -> bool:
        return _llama_available

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    @property
    def current_model_path(self) -> str:
        return self._current_path

    def load_model(
        self,
        model_path: str,
        n_ctx: int = 4096,
        n_gpu_layers: int = 0,
        n_threads: int = 4,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Load a GGUF model. Unloads the previous model first."""
        if not _llama_available:
            raise RuntimeError(
                "llama-cpp-python is not installed. "
                "Run: pip install llama-cpp-python"
            )

        with self._lock:
            if self._current_path == model_path and self._model is not None:
                return  # Already loaded

            if progress_callback:
                progress_callback(f"Unloading previous model...")
            self._unload()

            if progress_callback:
                progress_callback(f"Loading {model_path}...")

            force_mmq = False
            flash_attn = True

            if n_gpu_layers != 0:
                gpu_info = _detect_gpu_info()
                if gpu_info:
                    fallback = _needs_mmq_fallback(gpu_info["name"])
                    force_mmq = fallback
                    flash_attn = not fallback

                    if force_mmq:
                        os.environ["GGML_CUDA_FORCE_MMQ"] = "1"
                        logger.info(
                            "[llm_engine] GGML_CUDA_FORCE_MMQ=1 activado "
                            "(GPU sin Tensor Cores reales)"
                        )

                    logger.info(
                        f"[llm_engine] GPU: {gpu_info['name']} | "
                        f"VRAM: {gpu_info['mem_free']}/{gpu_info['mem_total']} MiB libre | "
                        f"CC: {gpu_info['compute_cap']} | "
                        f"force_mmq={force_mmq} | flash_attn={flash_attn}"
                    )
                else:
                    # No nvidia-smi / no GPU detected but n_gpu_layers requested —
                    # be conservative and assume no Tensor Cores.
                    force_mmq = True
                    flash_attn = False
                    os.environ["GGML_CUDA_FORCE_MMQ"] = "1"
                    logger.info(
                        "[llm_engine] GPU no detectada via nvidia-smi; "
                        "usando modo conservador (force_mmq=True, flash_attn=False)"
                    )

            logger.info(f"[main_llm] Modelo: {Path(model_path).stem}")

            self._model = Llama(
                model_path=model_path,
                n_ctx=n_ctx,
                n_gpu_layers=n_gpu_layers,
                n_threads=n_threads,
                flash_attn=flash_attn,
                verbose=False,
            )
            self._current_path = model_path

            if progress_callback:
                progress_callback(f"Model ready.")

    def _unload(self) -> None:
        if self._model is not None:
            del self._model
            self._model = None
            self._current_path = ""

    def unload(self) -> None:
        with self._lock:
            self._unload()

    def generate(
        self,
        messages: list[dict],
        max_tokens: int = 2048,
        temperature: float = 0.7,
        top_p: float = 0.9,
        stop: Optional[list[str]] = None,
        stream: bool = False,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Run inference.
        If stream=True and stream_callback is provided, calls stream_callback
        with each token as it arrives, and returns the full text at the end.
        """
        if not _llama_available:
            raise RuntimeError("llama-cpp-python is not installed.")
        if self._model is None:
            raise RuntimeError("No model loaded. Call load_model() first.")

        with self._lock:
            if stream and stream_callback:
                return self._stream_generate(
                    messages, max_tokens, temperature, top_p, stop, stream_callback
                )
            else:
                return self._blocking_generate(
                    messages, max_tokens, temperature, top_p, stop
                )

    def _blocking_generate(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        top_p: float,
        stop: Optional[list[str]],
    ) -> str:
        response = self._model.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop=stop or [],
            stream=False,
        )
        return response["choices"][0]["message"]["content"] or ""

    def _stream_generate(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        top_p: float,
        stop: Optional[list[str]],
        stream_callback: Callable[[str], None],
    ) -> str:
        chunks = []
        response_iter = self._model.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop=stop or [],
            stream=True,
        )
        for chunk in response_iter:
            delta = chunk["choices"][0]["delta"]
            token = delta.get("content", "")
            if token:
                chunks.append(token)
                stream_callback(token)
        return "".join(chunks)


# Global singleton
_engine = LLMEngine()


def get_engine() -> LLMEngine:
    return _engine
