"""
Single entry point for text generation, used by the workflow.

The router reads the model reference (engine.model_refs) and sends the work
to the matching backend:

  BackendKind.LOCAL  -> engine.chat.LLMEngine      (llama-cpp-python, GGUF files)
  BackendKind.OLLAMA -> engine.ollama_client        (Ollama server)
  BackendKind.CLOUD  -> engine.cloud_client         (OpenAI, Anthropic, ...)

Only one backend holds a model at a time, so switching backends frees the
previous one first. Every backend exposes the same methods, so callers never
need to know which one is active.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from engine import cloud_client, ollama_client
from engine.chat import _llama_available, get_local_engine
from engine.model_refs import BackendKind, kind_of, parse_cloud_ref
from engine.models import AppSettings


class LLMRouter:
    def __init__(self) -> None:
        self._backends = {
            BackendKind.LOCAL: get_local_engine(),
            BackendKind.OLLAMA: ollama_client.OllamaEngine(),
            BackendKind.CLOUD: cloud_client.CloudEngine(),
        }
        self._active: Optional[object] = None
        self._lock = threading.Lock()

    # -- availability ---------------------------------------------------

    def availability_error(self, ref: str) -> Optional[str]:
        """
        Why a model reference cannot be used on this machine, or None.
        Remote problems (server down, missing key) are reported by
        load_model() with the exact cause, so only local requirements and
        malformed references are checked here.
        """
        kind = kind_of(ref)
        if kind is BackendKind.LOCAL and not _llama_available:
            return (
                "Local GGUF models need llama-cpp-python, which is not installed.\n"
                "Run: pip install llama-cpp-python\n"
                "(Or use an Ollama or cloud model instead.)"
            )
        if kind is BackendKind.CLOUD and parse_cloud_ref(ref) is None:
            return f"Invalid cloud model reference: {ref}. Pick the model again in Text Models."
        return None

    # -- engine-like surface ---------------------------------------------

    @property
    def model_loaded(self) -> bool:
        return self._active is not None and self._active.model_loaded

    @property
    def current_model_path(self) -> str:
        return self._active.current_model_path if self._active else ""

    @property
    def current_context_size(self) -> int:
        return self._active.current_context_size if self._active else 0

    @property
    def context_size(self) -> int:
        return self.current_context_size

    @property
    def last_model_info(self):
        return self._active.last_model_info if self._active else None

    def load_model(
        self,
        model_ref: str,
        *,
        settings: Optional[AppSettings] = None,
        n_ctx: int = 4096,
        n_gpu_layers: int = 0,
        n_threads: int = 4,
        n_threads_batch: int = 0,
        moe_n_batch: int = 1024,
        moe_n_ubatch: int = 1024,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        kind = kind_of(model_ref)
        target = self._backends[kind]

        # Resolve everything that can fail on configuration BEFORE unloading
        # the current model, so a bad key or URL leaves the previous one intact.
        loader = self._prepare_loader(kind, model_ref, settings, n_ctx, n_gpu_layers,
                                      n_threads, n_threads_batch, moe_n_batch, moe_n_ubatch,
                                      progress_callback)

        with self._lock:
            for backend in self._backends.values():
                if backend is not target and backend.model_loaded:
                    backend.unload()
            loader()
            self._active = target

    def _prepare_loader(self, kind, ref, settings, n_ctx, n_gpu_layers, n_threads,
                        n_threads_batch, moe_n_batch, moe_n_ubatch, progress_callback):
        cloud_providers = getattr(settings, "cloud_providers", {}) if settings else {}
        base_url = getattr(settings, "ollama_base_url", ollama_client.DEFAULT_OLLAMA_URL)

        if kind is BackendKind.OLLAMA:
            backend = self._backends[kind]
            return lambda: backend.load_model(
                ref, base_url=base_url, n_ctx=n_ctx, progress_callback=progress_callback,
            )

        if kind is BackendKind.CLOUD:
            parsed = parse_cloud_ref(ref)
            if parsed is None:
                raise cloud_client.CloudError(f"Invalid cloud model reference: {ref}")
            provider_id, model = parsed
            conn = cloud_client.connection_for(cloud_providers, provider_id)
            backend = self._backends[kind]
            return lambda: backend.load_model(
                ref, conn, model, n_ctx=n_ctx, progress_callback=progress_callback,
            )

        backend = self._backends[kind]
        return lambda: backend.load_model(
            ref,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            n_threads=n_threads,
            n_threads_batch=n_threads_batch,
            moe_n_batch=moe_n_batch,
            moe_n_ubatch=moe_n_ubatch,
            progress_callback=progress_callback,
        )

    def unload(self) -> None:
        with self._lock:
            for backend in self._backends.values():
                backend.unload()
            self._active = None

    def generate(
        self,
        messages: list[dict],
        max_tokens: int = 2048,
        temperature: float = 0.7,
        top_p: float = 0.9,
        top_k: int = 40,
        stop: Optional[list[str]] = None,
        stream: bool = False,
        stream_callback: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> str:
        active = self._active
        if active is None or not active.model_loaded:
            raise RuntimeError("No model loaded. Call load_model() first.")
        return active.generate(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop=stop,
            stream=stream,
            stream_callback=stream_callback,
            cancel_check=cancel_check,
        )


_router = LLMRouter()


def get_engine() -> LLMRouter:
    return _router
