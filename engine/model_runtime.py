"""Model-level runtime configuration for local GGUF models.

The model assignment belongs to the project. Runtime options belong to the
model itself, so one model can be assigned to many tasks without duplicating
its runtime configuration.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelRuntimeProfile:
    """Runtime options shared by every task using the same model."""

    mtp_model_path: str = ""

    @property
    def uses_mtp(self) -> bool:
        return bool(self.mtp_model_path.strip())


def normalize_model_path(path: str) -> str:
    """Return a stable absolute/case-insensitive key for a model path."""
    return os.path.normcase(os.path.abspath(os.path.expanduser(path.strip())))


def get_model_runtime_profile(settings: Any, model_path: str) -> ModelRuntimeProfile:
    """Read the single runtime profile stored for a target model."""
    raw_profiles = getattr(settings, "model_runtime_profiles", {}) or {}
    key = normalize_model_path(model_path)
    raw = raw_profiles.get(key, {})
    if not isinstance(raw, dict):
        return ModelRuntimeProfile()
    return ModelRuntimeProfile(
        mtp_model_path=str(raw.get("mtp_model_path", "") or "").strip()
    )


def set_model_mtp_path(settings: Any, model_path: str, mtp_model_path: str) -> None:
    """Set or clear the MTP drafter for one target model."""
    key = normalize_model_path(model_path)
    profiles = dict(getattr(settings, "model_runtime_profiles", {}) or {})
    mtp = str(mtp_model_path or "").strip()

    if not mtp:
        profiles.pop(key, None)
    else:
        profiles[key] = {"mtp_model_path": os.path.abspath(os.path.expanduser(mtp))}

    settings.model_runtime_profiles = profiles


def find_llama_cli(explicit_path: str = "") -> str:
    """Locate llama-cli.exe without assuming a particular installation folder."""
    candidates: list[Path] = []

    explicit = str(explicit_path or "").strip()
    if explicit:
        candidates.append(Path(explicit))

    env_path = os.environ.get("LLAMA_CPP_CLI_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))

    exe_name = "llama-cli.exe" if os.name == "nt" else "llama-cli"

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())

    found = shutil.which(exe_name)
    if found:
        return str(Path(found).resolve())

    for base in (Path.cwd(), Path(sys.executable).resolve().parent):
        candidate = base / exe_name
        if candidate.is_file():
            return str(candidate.resolve())

    return ""
