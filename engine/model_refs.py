"""
Model references: the single string that identifies a model in a task's
assignment (ModelAssignment) and tells the router which backend runs it.

  local GGUF file   "/path/to/model.gguf"            -> llama-cpp-python
  Ollama            "ollama:qwen3:8b"                -> Ollama server
  Cloud API         "cloud://anthropic/claude-..."   -> cloud provider

Everything that parses or prints a reference goes through this module, so
the UI, the workflow and the router can never disagree about the format.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

from engine.cloud_providers import get_preset

OLLAMA_PREFIX = "ollama:"
CLOUD_PREFIX = "cloud://"


class BackendKind(str, Enum):
    LOCAL = "local"
    OLLAMA = "ollama"
    CLOUD = "cloud"


def kind_of(ref: str) -> BackendKind:
    if ref.startswith(CLOUD_PREFIX):
        return BackendKind.CLOUD
    if ref.startswith(OLLAMA_PREFIX):
        return BackendKind.OLLAMA
    return BackendKind.LOCAL


def make_ollama_ref(name: str) -> str:
    return f"{OLLAMA_PREFIX}{name}"


def ollama_model_name(ref: str) -> str:
    return ref[len(OLLAMA_PREFIX):] if kind_of(ref) is BackendKind.OLLAMA else ref


def make_cloud_ref(provider_id: str, model: str) -> str:
    return f"{CLOUD_PREFIX}{provider_id}/{model}"


def parse_cloud_ref(ref: str) -> Optional[tuple[str, str]]:
    """(provider_id, model) or None if the reference is not a cloud reference.
    Only the first '/' separates them, because model names may contain '/'
    (e.g. OpenRouter's 'meta-llama/llama-3.1-8b')."""
    if kind_of(ref) is not BackendKind.CLOUD:
        return None
    rest = ref[len(CLOUD_PREFIX):]
    provider_id, sep, model = rest.partition("/")
    if not sep or not provider_id or not model:
        return None
    return provider_id, model


def backend_tag(ref: str) -> str:
    kind = kind_of(ref)
    if kind is BackendKind.OLLAMA:
        return "[Ollama]"
    if kind is BackendKind.CLOUD:
        parsed = parse_cloud_ref(ref)
        preset = get_preset(parsed[0]) if parsed else None
        return f"[Cloud · {preset.label if preset else 'unknown'}]"
    return "[llama.cpp]"


def display_name(ref: str) -> str:
    """Unambiguous label for a model reference, e.g. '[Ollama] qwen3:8b'."""
    kind = kind_of(ref)
    if kind is BackendKind.OLLAMA:
        return f"{backend_tag(ref)} {ollama_model_name(ref)}"
    if kind is BackendKind.CLOUD:
        parsed = parse_cloud_ref(ref)
        return f"{backend_tag(ref)} {parsed[1] if parsed else ref}"
    return f"{backend_tag(ref)} {Path(ref).name}"


def is_local_file_missing(ref: str) -> bool:
    return kind_of(ref) is BackendKind.LOCAL and bool(ref) and not Path(ref).is_file()
