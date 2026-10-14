"""
Ollama backend.

Talks to an Ollama server over its HTTP API using only the standard library,
so no extra dependency is needed.

Model references
----------------
Every task's model assignment is a single string:

  * an absolute path to a ``.gguf`` file  -> local backend (llama-cpp-python)
  * ``"ollama:<model name>"``             -> Ollama server, e.g. ``"ollama:qwen3:8b"``

``engine.chat.get_engine()`` routes each request to the right backend, so the
workflow code never needs to know which one is in use.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("llm_engine")

OLLAMA_PREFIX = "ollama:"
DEFAULT_OLLAMA_URL = "http://localhost:11434"

# Generation on CPU can take a long time before the first token arrives.
_GENERATION_TIMEOUT = 900
# Quick calls (listing models, status checks) should fail fast.
_PROBE_TIMEOUT = 5


class OllamaError(RuntimeError):
    """Raised for any problem talking to the Ollama server."""


# ---------------------------------------------------------------------------
# Model reference helpers
# ---------------------------------------------------------------------------

def is_ollama_ref(ref: str) -> bool:
    return bool(ref) and ref.startswith(OLLAMA_PREFIX)


def make_ollama_ref(name: str) -> str:
    return f"{OLLAMA_PREFIX}{name}"


def ollama_model_name(ref: str) -> str:
    return ref[len(OLLAMA_PREFIX):] if is_ollama_ref(ref) else ref


def backend_label(ref: str) -> str:
    """Short backend tag shown in the UI next to every model."""
    return "[Ollama]" if is_ollama_ref(ref) else "[llama.cpp]"


def display_name(ref: str) -> str:
    """Unambiguous label for a model reference, e.g. '[Ollama] qwen3:8b'."""
    if is_ollama_ref(ref):
        return f"{backend_label(ref)} {ollama_model_name(ref)}"
    return f"{backend_label(ref)} {Path(ref).name}"


def normalize_base_url(base_url: str) -> str:
    url = (base_url or "").strip() or DEFAULT_OLLAMA_URL
    if "://" not in url:
        url = "http://" + url
    return url.rstrip("/")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post_or_get(base_url: str, path: str, payload: Optional[dict], timeout: float):
    url = normalize_base_url(base_url) + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return json.loads(body)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise OllamaError(f"Ollama returned HTTP {e.code} for {path}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise OllamaError(
            f"Could not reach Ollama at {url} ({e}). "
            "Make sure Ollama is running (`ollama serve`)."
        ) from e


def list_models(base_url: str, timeout: float = _PROBE_TIMEOUT) -> list[str]:
    """Names of the models installed on the Ollama server (e.g. 'qwen3:8b')."""
    data = _post_or_get(base_url, "/api/tags", None, timeout)
    names = [m.get("name", "") for m in data.get("models", [])]
    return sorted(n for n in names if n)


def check_server(base_url: str) -> tuple[bool, str]:
    """Status for the UI. Never raises."""
    try:
        names = list_models(base_url)
    except OllamaError as e:
        return False, str(e)
    count = len(names)
    return True, f"Connected — {count} model{'s' if count != 1 else ''} installed."


def _has_model(installed: list[str], name: str) -> bool:
    wanted = name if ":" in name else f"{name}:latest"
    return name in installed or wanted in installed


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class OllamaEngine:
    """
    Same surface as engine.chat.LLMEngine so the router can treat both alike.
    Only one Ollama model is considered "loaded" at a time; Ollama itself
    keeps models in memory for a while after each request.
    """

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
        # GGUF metadata only exists for local files.
        return None

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
                progress_callback(f"Checking Ollama model {name}...")

            installed = list_models(base)
            if not _has_model(installed, name):
                raise OllamaError(
                    f"Model '{name}' is not installed in Ollama. "
                    f"Run: ollama pull {name}"
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
        """Ask Ollama to free the current model (keep_alive=0)."""
        if not self._model_name:
            return
        try:
            _post_or_get(
                self._base_url,
                "/api/generate",
                {"model": self._model_name, "keep_alive": 0},
                _PROBE_TIMEOUT,
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

        with self._lock:
            payload = self._build_payload(
                messages, max_tokens, temperature, top_p, top_k, stop, stream
            )
            if stream and stream_callback:
                return self._stream(payload, stream_callback, cancel_check)
            return self._blocking(payload)

    def _build_payload(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        stop: Optional[list[str]],
        stream: bool,
    ) -> dict:
        options: dict = {
            "num_ctx": self._n_ctx,
            "num_predict": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        }
        if stop:
            options["stop"] = list(stop)

        is_qwen = "qwen" in self._model_name.lower()
        if is_qwen:
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
        if is_qwen:
            payload["think"] = _thinking_enabled()
        return payload

    def _blocking(self, payload: dict) -> str:
        data = _post_or_get(self._base_url, "/api/chat", payload, _GENERATION_TIMEOUT)
        if "error" in data:
            raise OllamaError(str(data["error"]))
        return (data.get("message") or {}).get("content", "") or ""

    def _stream(
        self,
        payload: dict,
        stream_callback: Callable[[str], None],
        cancel_check: Optional[Callable[[], bool]],
    ) -> str:
        url = self._base_url + "/api/chat"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        chunks: list[str] = []
        try:
            resp = urllib.request.urlopen(req, timeout=_GENERATION_TIMEOUT)
        except (urllib.error.URLError, OSError) as e:
            raise OllamaError(f"Could not reach Ollama at {url} ({e}).") from e

        with resp:
            for raw in resp:
                if cancel_check is not None and cancel_check():
                    logger.info("[ollama] Generation cancelled — closing stream.")
                    break
                line = raw.strip()
                if not line:
                    continue
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


def _thinking_enabled() -> bool:
    try:
        from engine import storage
        return bool(getattr(storage.load_settings(), "enable_thinking", False))
    except Exception:
        return False
