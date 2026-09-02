"""
Feature detection for the installed llama-cpp-python build.

llama-cpp-python's Llama() constructor exposes different keyword
arguments depending on which llama.cpp commit it was compiled against.
Rather than hard-coding assumptions about "the current version," this
introspects the actually-installed class at runtime and only ever
reports a parameter as usable if it's really there — this is the literal
implementation of "before using a parameter, verify the installed
version actually supports it; if it doesn't exist, don't use it."
"""

from __future__ import annotations

import inspect
import logging
from typing import Optional

logger = logging.getLogger("llama_features")

_supported_params: Optional[set] = None










_MOE_CPU_OFFLOAD_PARAM_CANDIDATES = ("n_cpu_moe", "cpu_moe")


def _llama_init_params() -> set:
    global _supported_params
    if _supported_params is not None:
        return _supported_params
    try:
        from llama_cpp import Llama
        _supported_params = set(inspect.signature(Llama.__init__).parameters.keys())
    except Exception as e:
        logger.warning(f"Could not introspect llama_cpp.Llama signature: {e}")
        _supported_params = set()
    return _supported_params


def reset_cache() -> None:
    """For tests, or if llama_cpp gets (re)installed during a running process."""
    global _supported_params
    _supported_params = None


def supports(param_name: str) -> bool:
    """True if the installed llama-cpp-python's Llama() accepts this kwarg."""
    return param_name in _llama_init_params()


def supports_chat_completion_param(param_name: str) -> bool:
    """True if create_chat_completion() accepts the given kwarg."""
    try:
        from llama_cpp import Llama
        params = inspect.signature(Llama.create_chat_completion).parameters.values()
        return any(
            p.kind is inspect.Parameter.VAR_KEYWORD or p.name == param_name
            for p in params
        )
    except Exception:
        return False


def moe_cpu_offload_param() -> Optional[str]:
    """
    Returns the actual kwarg name this installed version uses for
    CPU-offloading MoE expert tensors (mirroring llama.cpp's --cpu-moe /
    --n-cpu-moe), or None if this build doesn't expose one at all.
    """
    for name in _MOE_CPU_OFFLOAD_PARAM_CANDIDATES:
        if supports(name):
            return name
    return None


def supported_advanced_params() -> dict:
    """Snapshot of which optional kwargs this installed version supports —
    useful for logging/diagnostics."""
    candidates = [
        "flash_attn", "n_batch", "n_ubatch", "n_gpu_layers", "n_threads_batch",
        "offload_kqv", "split_mode", "main_gpu", "tensor_split",
        "n_cpu_moe", "cpu_moe",
    ]
    return {name: supports(name) for name in candidates}
