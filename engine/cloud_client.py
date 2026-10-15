"""
Cloud API backend.

Sends generation requests to hosted providers (OpenAI, Anthropic, Gemini,
OpenRouter, or any OpenAI-compatible server). It exposes the same surface as
the other backends so the router treats every backend alike.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from engine.cloud_providers import ProviderPreset, get_preset, provider_config
from engine.http_json import request_json, stream_lines

logger = logging.getLogger("llm_engine")

ANTHROPIC_VERSION = "2023-06-01"
_GENERATION_TIMEOUT = 900
_PROBE_TIMEOUT = 15
_OPENAI_MAX_STOP = 4  # the OpenAI API accepts at most four stop sequences


class CloudError(RuntimeError):
    """Raised for any problem talking to a cloud provider."""


@dataclass(frozen=True)
class ProviderConnection:
    preset: ProviderPreset
    api_key: str
    base_url: str

    @property
    def label(self) -> str:
        return self.preset.label


def connection_for(cloud_providers: dict, provider_id: str) -> ProviderConnection:
    """Resolve a provider's stored settings into a usable connection."""
    preset = get_preset(provider_id)
    if preset is None:
        raise CloudError(f"Unknown cloud provider '{provider_id}'.")
    config = provider_config(cloud_providers, provider_id)
    base_url = (config["base_url"] or preset.default_base_url).rstrip("/")
    if not base_url:
        raise CloudError(f"{preset.label} needs a base URL. Set it in Settings → Cloud APIs.")
    if preset.needs_api_key and not config["api_key"]:
        raise CloudError(
            f"No API key set for {preset.label}. Add it in Settings → Cloud APIs."
        )
    return ProviderConnection(preset=preset, api_key=config["api_key"], base_url=base_url)


def _headers(conn: ProviderConnection) -> dict:
    if conn.preset.wire == "anthropic":
        return {"x-api-key": conn.api_key, "anthropic-version": ANTHROPIC_VERSION}
    return {"Authorization": f"Bearer {conn.api_key}"} if conn.api_key else {}


def list_remote_models(conn: ProviderConnection) -> list[str]:
    """Model ids the provider reports for this key."""
    path = "/v1/models" if conn.preset.wire == "anthropic" else "/models"
    data = request_json(
        conn.base_url + path,
        error_cls=CloudError,
        headers=_headers(conn),
        timeout=_PROBE_TIMEOUT,
    )
    ids = [m.get("id", "") for m in data.get("data", [])]
    return sorted(i for i in ids if i)


def check_connection(conn: ProviderConnection) -> tuple[bool, str]:
    """Status for the UI. Never raises."""
    try:
        ids = list_remote_models(conn)
    except CloudError as e:
        return False, str(e)
    count = len(ids)
    return True, f"Connected — {count} model{'s' if count != 1 else ''} available."


# ---------------------------------------------------------------------------
# Request building / response parsing per wire format
# ---------------------------------------------------------------------------

def _openai_body(model: str, messages, max_tokens, temperature, top_p, top_k, stop, stream) -> dict:
    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stream": stream,
    }
    if stop:
        body["stop"] = list(stop)[:_OPENAI_MAX_STOP]
    return body


def _anthropic_body(model: str, messages, max_tokens, temperature, top_p, top_k, stop, stream) -> dict:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    convo = [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m["role"] in ("user", "assistant")
    ]
    if not convo or convo[0]["role"] != "user":
        convo.insert(0, {"role": "user", "content": "Continue."})

    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        # Anthropic accepts temperature in [0, 1]. top_p is left out on purpose:
        # newer Claude models reject setting both temperature and top_p.
        "temperature": min(max(temperature, 0.0), 1.0),
        "messages": convo,
        "stream": stream,
    }
    if system:
        body["system"] = system
    if stop:
        body["stop_sequences"] = list(stop)
    if top_k and top_k > 0:
        body["top_k"] = top_k
    return body


def _openai_text(data: dict) -> str:
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def _anthropic_text(data: dict) -> str:
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )


def _openai_stream_token(obj: dict) -> tuple[str, bool]:
    """(token, finished) for one OpenAI-format SSE event."""
    if "error" in obj:
        raise CloudError(str((obj["error"] or {}).get("message", obj["error"])))
    choices = obj.get("choices") or [{}]
    return (choices[0].get("delta") or {}).get("content") or "", False


def _anthropic_stream_token(obj: dict) -> tuple[str, bool]:
    kind = obj.get("type")
    if kind == "error":
        raise CloudError(str((obj.get("error") or {}).get("message", "Anthropic stream error")))
    if kind == "message_stop":
        return "", True
    if kind == "content_block_delta":
        delta = obj.get("delta") or {}
        if delta.get("type") == "text_delta":
            return delta.get("text", ""), False
    return "", False


_WIRES = {
    "openai": {
        "path": "/chat/completions",
        "body": _openai_body,
        "text": _openai_text,
        "token": _openai_stream_token,
    },
    "anthropic": {
        "path": "/v1/messages",
        "body": _anthropic_body,
        "text": _anthropic_text,
        "token": _anthropic_stream_token,
    },
}


class CloudEngine:
    """Same surface as LLMEngine / OllamaEngine. 'Loading' a cloud model only
    validates the connection; the model lives on the provider's side."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ref: str = ""
        self._model: str = ""
        self._conn: Optional[ProviderConnection] = None
        self._n_ctx: int = 0

    @property
    def model_loaded(self) -> bool:
        return bool(self._model and self._conn)

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
        return None

    def load_model(
        self,
        ref: str,
        connection: ProviderConnection,
        model: str,
        n_ctx: int = 4096,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        with self._lock:
            if progress_callback:
                progress_callback(f"Using {connection.label} · {model}")
            self._ref = ref
            self._model = model
            self._conn = connection
            self._n_ctx = n_ctx
            logger.info(f"[cloud] Ready: {connection.label} · {model}")

    def unload(self) -> None:
        with self._lock:
            self._ref = ""
            self._model = ""
            self._conn = None
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
        if not self.model_loaded:
            raise RuntimeError("No cloud model loaded. Call load_model() first.")

        conn = self._conn
        wire = _WIRES[conn.preset.wire]
        body = wire["body"](self._model, messages, max_tokens, temperature, top_p, top_k, stop, stream)
        url = conn.base_url + wire["path"]
        headers = _headers(conn)

        with self._lock:
            if stream and stream_callback:
                return self._stream(url, body, headers, wire["token"], stream_callback, cancel_check)
            data = request_json(url, error_cls=CloudError, payload=body, headers=headers,
                                timeout=_GENERATION_TIMEOUT)
            return wire["text"](data)

    def _stream(self, url, body, headers, parse_token, stream_callback, cancel_check) -> str:
        chunks: list[str] = []
        for line in stream_lines(
            url,
            error_cls=CloudError,
            payload=body,
            headers=headers,
            timeout=_GENERATION_TIMEOUT,
            should_stop=cancel_check,
        ):
            if not line.startswith("data:"):
                continue  # SSE event names and keep-alive comments
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            token, finished = parse_token(json.loads(payload))
            if token:
                chunks.append(token)
                stream_callback(token)
            if finished:
                break
        return "".join(chunks)
