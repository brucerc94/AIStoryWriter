from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from llama_cpp import Llama
except Exception:
    Llama = None


@dataclass
class GGUFModelInfo:
    architecture: str = "unknown"
    expert_count: int = 0
    expert_used_count: int = 0
    block_count: int = 0

    @property
    def is_moe(self) -> bool:
        return self.expert_count > 1


def _find_key(metadata: dict, *names):
    for name in names:
        if name in metadata:
            return metadata[name]
    return None


def read_model_info(model_path: str | Path) -> Optional[GGUFModelInfo]:
    """
    Lee únicamente los metadatos del GGUF.
    No carga los pesos del modelo.
    """

    if Llama is None:
        return None

    try:
        llm = Llama(
            model_path=str(model_path),
            vocab_only=True,
            verbose=False,
        )

        metadata = getattr(llm, "metadata", None)

        if metadata is None:
            metadata = {}

        arch = (
            _find_key(metadata, "general.architecture")
            or "unknown"
        )

        experts = (
            _find_key(
                metadata,
                f"{arch}.expert_count",
                "expert_count",
            )
            or 0
        )

        experts_used = (
            _find_key(
                metadata,
                f"{arch}.expert_used_count",
                "expert_used_count",
            )
            or 0
        )

        blocks = (
            _find_key(
                metadata,
                f"{arch}.block_count",
                "block_count",
            )
            or 0
        )

        del llm

        return GGUFModelInfo(
            architecture=str(arch),
            expert_count=int(experts),
            expert_used_count=int(experts_used),
            block_count=int(blocks),
        )

    except Exception:
        return None