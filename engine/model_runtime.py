"""
Model runtime configuration and discovery helpers.

A model may optionally have an execution profile.  The current supported
optimization is MTP (draft-mtp) speculative decoding through llama.cpp's
llama-server runtime.

The profile is keyed by the target model path, so the MTP configuration is
stored once per model rather than duplicated for every task assignment.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModelRuntimeProfile:
    """Runtime-only options attached to one target model."""

    mtp_model_path: str = ""

    @property
    def uses_mtp(self) -> bool:
        return bool(self.mtp_model_path.strip())


def normalize_model_path(path: str) -> str:
    """Return a stable case-insensitive absolute key on Windows."""
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(os.path.expanduser(path.strip())))


def get_model_runtime_profile(settings, model_path: str) -> ModelRuntimeProfile:
    """Read the runtime profile for a model without mutating settings."""
    key = normalize_model_path(model_path)
    if not key or settings is None:
        return ModelRuntimeProfile()

    profiles = getattr(settings, "model_runtime_profiles", None) or {}
    raw = profiles.get(key)
    if raw is None:
        # Backward/portable compatibility: tolerate entries saved with a
        # differently normalized path by comparing normalized keys once.
        for stored_key, value in profiles.items():
            if normalize_model_path(stored_key) == key:
                raw = value
                break

    if not isinstance(raw, dict):
        return ModelRuntimeProfile()

    return ModelRuntimeProfile(
        mtp_model_path=str(raw.get("mtp_model_path", "") or "").strip()
    )


def set_model_mtp_path(settings, model_path: str, mtp_model_path: str) -> None:
    """Set or clear the MTP path for one target model."""
    key = normalize_model_path(model_path)
    if not key:
        return

    if not hasattr(settings, "model_runtime_profiles") or settings.model_runtime_profiles is None:
        settings.model_runtime_profiles = {}

    if mtp_model_path.strip():
        settings.model_runtime_profiles[key] = {
            "mtp_model_path": os.path.abspath(os.path.expanduser(mtp_model_path.strip()))
        }
    else:
        settings.model_runtime_profiles.pop(key, None)


def find_llama_server(explicit_path: str = "") -> Optional[str]:
    """Find a llama-server executable without requiring a hard-coded install path."""
    candidates = []

    if explicit_path and explicit_path.strip():
        candidates.append(explicit_path.strip())

    env_path = os.environ.get("LLAMA_SERVER_PATH", "").strip()
    if env_path:
        candidates.append(env_path)

    candidates.extend(
        [
            os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "llama-server.exe"),
            os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "llama-server"),
            os.path.join(os.getcwd(), "llama-server.exe"),
            os.path.join(os.getcwd(), "llama-server"),
        ]
    )

    for name in ("llama-server.exe", "llama-server"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        candidate = os.path.abspath(candidate)
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate):
            return candidate

    return None
