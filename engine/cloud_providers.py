"""
Cloud API providers the app knows how to talk to.

Each preset says which wire format the provider speaks:

  * "openai"    — OpenAI Chat Completions format. Also used by Gemini's
                  OpenAI-compatible endpoint, OpenRouter, and any server that
                  implements the same API (LM Studio, vLLM, Groq, ...).
  * "anthropic" — Anthropic Messages format (Claude).

The user only supplies an API key (and optionally a base URL / model list).
Keys and per-provider settings live in AppSettings.cloud_providers, which is
encrypted at rest together with the rest of settings.json.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    label: str
    wire: str                 # "openai" | "anthropic"
    default_base_url: str
    needs_api_key: bool = True


PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset("openai", "OpenAI", "openai", "https://api.openai.com/v1"),
    ProviderPreset("anthropic", "Anthropic (Claude)", "anthropic", "https://api.anthropic.com"),
    ProviderPreset(
        "gemini", "Google Gemini", "openai",
        "https://generativelanguage.googleapis.com/v1beta/openai",
    ),
    ProviderPreset("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1"),
    ProviderPreset(
        "custom", "Custom OpenAI-compatible server", "openai", "",
        needs_api_key=False,
    ),
)

_BY_ID = {p.id: p for p in PRESETS}


def get_preset(provider_id: str) -> Optional[ProviderPreset]:
    return _BY_ID.get(provider_id)


def default_config() -> dict:
    return {"api_key": "", "base_url": "", "models": []}


def provider_config(cloud_providers: dict, provider_id: str) -> dict:
    """Stored config for a provider, merged over defaults. Never raises."""
    stored = (cloud_providers or {}).get(provider_id) or {}
    config = default_config()
    config["api_key"] = str(stored.get("api_key", "") or "")
    config["base_url"] = str(stored.get("base_url", "") or "")
    models = stored.get("models", []) or []
    config["models"] = [str(m) for m in models if str(m).strip()]
    return config
