"""
What models the user can pick, and where each one comes from.

Three sources feed the Text Models tab:

  * GGUF files in the models folder     (local, llama.cpp)
  * models installed on an Ollama server (local server, Ollama)
  * models listed for each configured cloud provider (cloud API)

Each source reports its own status, so the UI can say exactly what is
missing (no folder, server not running, no API key) instead of showing one
mixed, unexplained list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine import ollama_client, storage
from engine.cloud_providers import PRESETS, provider_config
from engine.model_refs import make_cloud_ref, make_ollama_ref
from engine.models import AppSettings

_OLLAMA_CATALOG_TIMEOUT = 2  # the UI calls this on settings changes; keep it short


@dataclass(frozen=True)
class SourceStatus:
    key: str                       # "gguf" | "ollama" | "cloud"
    title: str
    ok: bool
    message: str
    refs: tuple[str, ...] = field(default_factory=tuple)


def build_catalog(settings: Optional[AppSettings]) -> list[SourceStatus]:
    if settings is None:
        settings = AppSettings()
    return [
        _gguf_status(settings.models_directory),
        _ollama_status(settings.ollama_base_url),
        _cloud_status(settings.cloud_providers),
    ]


def all_refs(statuses: list[SourceStatus]) -> list[str]:
    refs: list[str] = []
    for status in statuses:
        refs.extend(status.refs)
    return refs


def _gguf_status(models_dir: str) -> SourceStatus:
    title = "GGUF files (llama.cpp)"
    if not models_dir:
        return SourceStatus("gguf", title, False, "No folder set. Choose it in Settings.")
    files = storage.list_gguf_models(models_dir)
    if not files:
        return SourceStatus("gguf", title, False, f"No .gguf files found in {models_dir}.")
    return SourceStatus(
        "gguf", title, True,
        f"{len(files)} model{'s' if len(files) != 1 else ''} in {models_dir}",
        tuple(files),
    )


def _ollama_status(base_url: str) -> SourceStatus:
    title = "Ollama"
    url = ollama_client.normalize_base_url(base_url)
    try:
        names = ollama_client.list_models(url, timeout=_OLLAMA_CATALOG_TIMEOUT)
    except ollama_client.OllamaError:
        return SourceStatus(
            "ollama", title, False,
            f"Not running at {url}. Start Ollama, or change the URL in Settings.",
        )
    refs = tuple(make_ollama_ref(n) for n in names)
    if not refs:
        return SourceStatus("ollama", title, False, f"Running at {url}, but no models installed.")
    return SourceStatus(
        "ollama", title, True,
        f"{len(refs)} model{'s' if len(refs) != 1 else ''} at {url}",
        refs,
    )


def _cloud_status(cloud_providers: dict) -> SourceStatus:
    title = "Cloud APIs"
    refs: list[str] = []
    providers_used = 0
    for preset in PRESETS:
        config = provider_config(cloud_providers, preset.id)
        has_access = bool(config["api_key"]) if preset.needs_api_key else bool(
            config["base_url"] or config["models"]
        )
        if not has_access or not config["models"]:
            continue
        providers_used += 1
        refs.extend(make_cloud_ref(preset.id, m) for m in config["models"])

    if not refs:
        return SourceStatus(
            "cloud", title, False,
            "No provider ready. Add an API key and model names in Settings → Cloud APIs.",
        )
    return SourceStatus(
        "cloud", title, True,
        f"{len(refs)} model{'s' if len(refs) != 1 else ''} from {providers_used} provider"
        f"{'s' if providers_used != 1 else ''}",
        tuple(refs),
    )
