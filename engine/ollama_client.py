"""
Ollama backend: talks to an Ollama server over its HTTP API.

Model references for Ollama models look like "ollama:<name>" (see
engine.model_refs). The router decides when this backend is used.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Optional

from engine.http_json import request_json, stream_lines
from engine.model_refs import ollama_model_name

logger = logging.getLogger("llm_engine")

DEFAULT_OLLAMA_URL = "http://localhost:11434"
_GENERATION_TIMEOUT = 900  # CPU generation can be slow before the first token
_PROBE_TIMEOUT = 5


class OllamaError(RuntimeError):
    """Raised for any problem talking to the Ollama server."""


def normalize_base_url(base_url: str) -> str:
    url = (base_url or "").strip() or DEFAULT_OLLAMA_URL
    if "://" not in url:
        url = "http://" + url
    return url.rstrip("/")


def list_models(base_url: str, timeout: float = _PROBE_TIMEOUT) -> list[str]:
    """Names of the models installed on the server (e.g. 'qwen3:8b')."""
    data = request_json(
        normalize_base_url(base_url) + "/api/tags",
        error_cls=OllamaError,
        timeout=timeout,
    )
    names = [m.get("name", "") for m in data.get("models", [])]
    return sorted(n for n in names if n)


def check_server(base_url: str) -> tuple[bool, str]:
    """Status for the UI. Never raises."""
    try:
        names = list_models(base_url)
    except OllamaError as e:
        return False, f"{e} Make sure Ollama is running (`ollama serve`)."
    count = len(names)
    return True, f"Connected — {count} model{'s' if count != 1 else ''} installed."


def _has_model(installed: list[str], name: str) -> bool:
    wanted = name if ":" in name else f"{name}:latest"
    return name in installed or wanted in installed


def _is_qwen(name: str) -> bool:
    return "qwen" in name.lower()


def _thinking_enabled() -> bool:
    try:
        from engine import storage
        return bool(getattr(storage.load_settings(), "enable_thinking", False))
    except Exception:
        return False


class OllamaEngine:
    """Same surface as LLMEngine / CloudEngine. Ollama keeps models in memory
    for a while on its own; unloading asks it to free the model right away."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ref: str = ""
        self._model_name: str = ""
        self._base_url: str = DEFAULT_OLLAMA_URL
        self._n_ctx: int = 0

    @property
    def model_loaded(self) -> bool:
        return bool(self._model_name)

    @property
    def current_model_path(self) -> str:
        return self._ref

    @property
    def current_context_size(self) -> int:
        return self._n_ctx

    @property
    def context_size(self) -> int:
        return self._n_ctx

    @property
    def last_model_info(self):
        return None  # GGUF metadata only exists for local files

    def load_model(
        self,
        ref: str,
        base_url: str,
        n_ctx: int = 4096,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        name = ollama_model_name(ref)
        base = normalize_base_url(base_url)

        with self._lock:
            if self._ref == ref and self._base_url == base and self._model_name:
                self._n_ctx = n_ctx
                return

            if progress_callback:
                progress_callback(f"Checking Ollama model {name}…")
            installed = list_models(base)
            if not _has_model(installed, name):
                raise OllamaError(
                    f"Model '{name}' is not installed in Ollama. Run: ollama pull {name}"
                )

            self._unload_remote()
            self._ref = ref
            self._model_name = name
            self._base_url = base
            self._n_ctx = n_ctx
            logger.info(f"[ollama] Model ready: {name} at {base} (num_ctx={n_ctx})")
            if progress_callback:
                progress_callback("Model ready.")

    def unload(self) -> None:
        with self._lock:
            self._unload_remote()

    def _unload_remote(self) -> None:
        if not self._model_name:
            return
        try:
            request_json(
                self._base_url + "/api/generate",
                error_cls=OllamaError,
                payload={"model": self._model_name, "keep_alive": 0},
                timeout=_PROBE_TIMEOUT,
            )
        except OllamaError as e:
            logger.debug(f"[ollama] Could not unload {self._model_name}: {e}")
        self._ref = ""
        self._model_name = ""
        self._n_ctx = 0

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
        if not self._model_name:
            raise RuntimeError("No Ollama model loaded. Call load_model() first.")

        payload = self._build_payload(messages, max_tokens, temperature, top_p, top_k, stop, stream)
        url = self._base_url + "/api/chat"

        with self._lock:
            if stream and stream_callback:
                return self._stream(url, payload, stream_callback, cancel_check)
            data = request_json(url, error_cls=OllamaError, payload=payload, timeout=_GENERATION_TIMEOUT)
            if "error" in data:
                raise OllamaError(str(data["error"]))
            return (data.get("message") or {}).get("content", "") or ""

    def _build_payload(self, messages, max_tokens, temperature, top_p, top_k, stop, stream) -> dict:
        options: dict = {
            "num_ctx": self._n_ctx,
            "num_predict": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        }
        if stop:
            options["stop"] = list(stop)

        qwen = _is_qwen(self._model_name)
        if qwen:
            # Same anti-repetition defaults the local backend uses for Qwen.
            options["presence_penalty"] = 1.5
            options["repeat_penalty"] = 1.0

        payload: dict = {
            "model": self._model_name,
            "messages": messages,
            "stream": stream,
            "options": options,
            "keep_alive": "10m",
        }
        if qwen:
            payload["think"] = _thinking_enabled()
        return payload

    def _stream(self, url, payload, stream_callback, cancel_check) -> str:
        chunks: list[str] = []
        for line in stream_lines(
            url,
            error_cls=OllamaError,
            payload=payload,
            timeout=_GENERATION_TIMEOUT,
            should_stop=cancel_check,
        ):
            obj = json.loads(line)
            if "error" in obj:
                raise OllamaError(str(obj["error"]))
            token = (obj.get("message") or {}).get("content", "")
            if token:
                chunks.append(token)
                stream_callback(token)
            if obj.get("done"):
                break
        return "".join(chunks)
