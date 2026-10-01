"""
LLM inference engine.
Wraps llama-cpp-python. Manages model loading and unloading.
One model loaded at a time to conserve RAM.

Also auto-detects the NVIDIA GPU (via nvidia-smi) and, for cards without
real Tensor Cores (e.g. the GTX 16-series, Compute Capability 7.5 but no
Tensor Cores unlike RTX 20-series which shares the same CC), forces
GGML_CUDA_FORCE_MMQ=1 and disables flash attention — flash attention and
the default cuBLAS/Tensor Core matmul kernels are slower or unsupported
on those GPUs.

Mixture-of-Experts (MoE) handling
----------------------------------
Before loading, the GGUF file's header is inspected (engine.gguf_meta —
cheap, metadata-only read, no tensor data) to detect whether the model is
MoE (Qwen MoE variants, Mixtral, GPT-OSS-style MoE checkpoints, etc.) via
its `{arch}.expert_count` metadata. Dense models are completely unaffected
by any of this — the exact same load_model() call path and defaults as
before apply to them.

When a MoE model IS detected, three optimizations are applied automatically,
each independently guarded so a model that only benefits from one of them
still gets it even if another doesn't apply:

1. CPU-offloading MoE expert tensors (mirrors llama.cpp's --cpu-moe /
   --n-cpu-moe CLI flags), via engine.llama_features, which introspects
   the ACTUALLY INSTALLED llama-cpp-python's Llama() signature at runtime.
   This is only ever used if that introspection finds the installed build
   really exposes it — never assumed. As of this writing that feature
   lives in llama.cpp's CLI arg-parsing layer built on the lower-level
   tensor_buft_overrides mechanism, so many llama-cpp-python releases may
   not expose it as a plain kwarg yet; if not found, this step is a no-op
   and everything else still applies.
2. Larger n_batch/n_ubatch — MoE decoding amortizes per-token expert
   routing overhead much better with bigger batches than dense models do,
   so batch size is bumped for MoE (only if n_batch/n_ubatch are actually
   supported kwargs on the installed version).
3. The existing GPU/Tensor-Core-aware flash_attn + GGML_CUDA_FORCE_MMQ
   logic already applies to every model; no separate MoE-specific
   variant is needed there — the same "does this GPU actually have
   Tensor Cores" check is architecture-agnostic.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, Iterator, Optional

from sympy import true

from engine import gguf_meta, llama_features, storage
from engine.llama_cpp_runtime import LlamaCppCliRuntime
from engine.model_runtime import find_llama_cli, get_model_runtime_profile

_llama_available = False
try:
    from llama_cpp import Llama
    _llama_available = True
except ImportError:
    pass


logger = logging.getLogger("llm_engine")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False





_NO_TENSOR_CORE_MARKERS = ("GTX 16", "GTX 10", "GTX 9", "GTX 7")




_MOE_N_BATCH = 1024
_MOE_N_UBATCH = 1024






_DEFAULT_MOE_CPU_LAYERS = 999


def _detect_gpu_info() -> Optional[dict]:
    """Query the first NVIDIA GPU via nvidia-smi. Returns None if unavailable."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        line = result.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            return None
        name, mem_total, mem_free, compute_cap = parts[:4]
        return {
            "name": name,
            "mem_total": int(float(mem_total)),
            "mem_free": int(float(mem_free)),
            "compute_cap": compute_cap,
        }
    except Exception:
        return None


def _needs_mmq_fallback(gpu_name: str) -> bool:
    """True for GPUs with no real Tensor Cores (needs MMQ, no flash attention)."""
    upper = gpu_name.upper()
    return any(marker in upper for marker in _NO_TENSOR_CORE_MARKERS)


class LLMEngine:
    """
    Singleton-style LLM engine.
    Loads a GGUF model on demand and unloads the previous one
    when a new model path is requested.
    """

    def __init__(self) -> None:
        self._model: Optional[object] = None
        self._current_path: str = ""
        self._current_n_ctx: int = 0
        self._lock = threading.Lock()
        self._last_model_info: Optional[gguf_meta.GGUFModelInfo] = None
        self._runtime_mode: str = "llama-cpp-python"
        self._mtp_runtime = LlamaCppCliRuntime()
        self._mtp_model_path: str = ""
        self._llama_cli_path: str = ""
        self._mtp_flash_attn: bool = False

    @property
    def is_available(self) -> bool:
        if _llama_available:
            return True
        try:
            settings = storage.load_settings()
            return bool(find_llama_cli(getattr(settings, "llama_cpp_cli_path", "")))
        except Exception:
            return False

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    @property
    def current_model_path(self) -> str:
        return self._current_path

    @property
    def current_context_size(self) -> int:
        """Effective context window used to load the current model."""
        return self._current_n_ctx

    @property
    def context_size(self) -> int:
        """Alias for the currently loaded model context window."""
        return self._current_n_ctx

    @property
    def last_model_info(self) -> Optional[gguf_meta.GGUFModelInfo]:
        """Architecture/MoE metadata detected for the currently loaded model."""
        return self._last_model_info

    def load_model(
        self,
        model_path: str,
        n_ctx: int = 4096,
        n_gpu_layers: int = 0,
        n_threads: int = 4,
        n_threads_batch: int = 0,
        moe_n_batch: int = _MOE_N_BATCH,
        moe_n_ubatch: int = _MOE_N_UBATCH,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Load a GGUF model. Unloads the previous model first.

        n_threads_batch: threads used for prompt/batch processing, separate
        from n_threads (single-token generation). 0 = don't pass it at all,
        which is llama-cpp-python's own "mirror n_threads" default — not
        forced here, so builds that pick a different internal default still
        get it.

        moe_n_batch / moe_n_ubatch: batch/micro-batch size applied ONLY when
        this model is detected as MoE (see module docstring) — ignored
        entirely for dense models.
        """
        with self._lock:
            if self._current_path == model_path and self._model is not None:
                return

            if progress_callback:
                progress_callback(f"Unloading previous model...")
            self._unload()

            if progress_callback:
                progress_callback(f"Loading {model_path}...")

            force_mmq = False
            flash_attn = True

            if n_gpu_layers != 0:
                gpu_info = _detect_gpu_info()
                if gpu_info:
                    fallback = _needs_mmq_fallback(gpu_info["name"])
                    force_mmq = fallback
                    flash_attn = not fallback

                    if force_mmq:
                        os.environ["GGML_CUDA_FORCE_MMQ"] = "1"
                        logger.info(
                            "[llm_engine] GGML_CUDA_FORCE_MMQ=1 activado "
                            "(GPU sin Tensor Cores reales)"
                        )

                    logger.info(
                        f"[llm_engine] GPU: {gpu_info['name']} | "
                        f"VRAM: {gpu_info['mem_free']}/{gpu_info['mem_total']} MiB libre | "
                        f"CC: {gpu_info['compute_cap']} | "
                        f"force_mmq={force_mmq} | flash_attn={flash_attn}"
                    )
                else:


                    force_mmq = True
                    flash_attn = False
                    os.environ["GGML_CUDA_FORCE_MMQ"] = "1"
                    logger.info(
                        "[llm_engine] GPU no detectada via nvidia-smi; "
                        "usando modo conservador (force_mmq=True, flash_attn=False)"
                    )

            logger.info(f"[main_llm] Modelo: {Path(model_path).stem}")


            model_info = gguf_meta.read_model_info(model_path)
            self._last_model_info = model_info

            settings = storage.load_settings()
            runtime_profile = get_model_runtime_profile(settings, model_path)

            if runtime_profile.uses_mtp:
                mtp_path = runtime_profile.mtp_model_path
                if not Path(mtp_path).is_file():
                    raise RuntimeError(
                        f"MTP Draft GGUF not found: {mtp_path}"
                    )

                cli_path = find_llama_cli(
                    getattr(settings, "llama_cpp_cli_path", "")
                )
                if not cli_path:
                    raise RuntimeError(
                        "MTP is configured for this model, but llama-cli.exe "
                        "was not found. Configure llama_cpp_cli_path in Models."
                    )

                self._runtime_mode = "llama-cli-mtp"
                self._mtp_model_path = mtp_path
                self._llama_cli_path = cli_path
                self._mtp_flash_attn = flash_attn
                self._model = object()
                self._current_path = model_path
                self._current_n_ctx = n_ctx

                logger.info(
                    "[llm_engine] Native llama.cpp MTP enabled: target=%s | draft=%s | cli=%s",
                    Path(model_path).name,
                    Path(mtp_path).name,
                    Path(cli_path).name,
                )

                if progress_callback:
                    progress_callback("Native llama.cpp MTP ready.")

                return

            self._runtime_mode = "llama-cpp-python"
            self._mtp_model_path = ""
            self._llama_cli_path = ""
            self._mtp_flash_attn = False

            if not _llama_available:
                raise RuntimeError(
                    "llama-cpp-python is not installed. "
                    "Run: pip install llama-cpp-python"
                )

            extra_kwargs: dict = {}

            if model_info and model_info.is_moe:
                logger.info(
                    f"[llm_engine] Modelo MoE detectado: arch={model_info.architecture} "
                    f"| expertos={model_info.expert_count} "
                    f"(activos/token={model_info.expert_used_count or '?'}) "
                    f"| capas={model_info.block_count or '?'}"
                )




                moe_param = llama_features.moe_cpu_offload_param()
                if moe_param:
                    extra_kwargs[moe_param] = _DEFAULT_MOE_CPU_LAYERS
                    logger.info(
                        f"[llm_engine] MoE optimization: usando '{moe_param}="
                        f"{_DEFAULT_MOE_CPU_LAYERS}' (equivalente a --cpu-moe) "
                        "para descargar expertos a CPU y liberar VRAM."
                    )
                else:
                    logger.info(
                        "[llm_engine] MoE optimization: esta version de "
                        "llama-cpp-python no expone n_cpu_moe/cpu_moe — se "
                        "omite ese parametro especifico (no existe, no se usa)."
                    )


                if llama_features.supports("n_batch"):
                    extra_kwargs["n_batch"] = moe_n_batch
                if llama_features.supports("n_ubatch"):
                    extra_kwargs["n_ubatch"] = moe_n_ubatch
                if "n_batch" in extra_kwargs or "n_ubatch" in extra_kwargs:
                    logger.info(
                        f"[llm_engine] MoE optimization: n_batch="
                        f"{extra_kwargs.get('n_batch', 'default')}, "
                        f"n_ubatch={extra_kwargs.get('n_ubatch', 'default')} "
                        "(lotes mas grandes amortizan mejor el ruteo de "
                        "expertos por token en modelos MoE)."
                    )




            elif model_info:
                logger.info(
                    f"[llm_engine] Modelo Dense detectado: arch={model_info.architecture} "
                    f"| capas={model_info.block_count or '?'} — sin cambios de comportamiento."
                )




            if llama_features.supports("flash_attn"):
                extra_kwargs.setdefault("flash_attn", flash_attn)




            if n_threads_batch > 0 and llama_features.supports("n_threads_batch"):
                extra_kwargs["n_threads_batch"] = n_threads_batch

            self._model = Llama(
                model_path=model_path,
                n_ctx=n_ctx,
                n_gpu_layers=n_gpu_layers,
                n_threads=n_threads,
                verbose=true,
                **extra_kwargs,
            )
            self._current_path = model_path
            self._current_n_ctx = n_ctx

            if progress_callback:
                progress_callback(f"Model ready.")

    def _unload(self) -> None:
        self._mtp_runtime.stop()
        if self._model is not None:
            del self._model
            self._model = None
        self._current_path = ""
        self._current_n_ctx = 0
        self._runtime_mode = "llama-cpp-python"
        self._mtp_model_path = ""
        self._llama_cli_path = ""
        self._mtp_flash_attn = False

    def _model_supports_thinking(self) -> bool:
        """
        True when the loaded GGUF looks like a Qwen model and the installed
        llama-cpp-python exposes chat_template_kwargs on create_chat_completion().
        """
        model_info = self._last_model_info
        if not model_info:
            return False
        arch = (model_info.architecture or "").lower()
        if not arch.startswith("qwen"):
            return False
        return llama_features.supports_chat_completion_param("chat_template_kwargs")

    def _chat_template_kwargs(self) -> Optional[dict]:
        if not self._model_supports_thinking():
            return None
        try:
            from engine import storage
            settings = storage.load_settings()
            return {"enable_thinking": bool(getattr(settings, "enable_thinking", False))}
        except Exception:
            return {"enable_thinking": False}

    def _qwen_sampling_kwargs(self) -> dict:
        """
        Apply Qwen3.5's anti-repetition sampling defaults only to Qwen models.

        Qwen recommends presence_penalty=1.5 and repetition_penalty=1.0 for
        general generation. llama-cpp-python exposes the latter as
        repeat_penalty. Feature detection keeps older builds safe: an
        unsupported parameter is simply omitted instead of breaking generation.
        """
        model_info = self._last_model_info
        if not model_info:
            return {}

        arch = (model_info.architecture or "").lower()
        if not arch.startswith("qwen"):
            return {}

        sampling: dict = {}

        if llama_features.supports_chat_completion_param("presence_penalty"):
            sampling["presence_penalty"] = 1.5
        if llama_features.supports_chat_completion_param("repeat_penalty"):
            sampling["repeat_penalty"] = 1.0

        if sampling:
            logger.info(
                "[llm_engine] Qwen sampling: presence_penalty=%s, repeat_penalty=%s",
                sampling.get("presence_penalty", "unsupported"),
                sampling.get("repeat_penalty", "unsupported"),
            )

        return sampling

    def _create_chat_completion(self, **kwargs):
        chat_template_kwargs = self._chat_template_kwargs()
        if chat_template_kwargs is not None:
            kwargs["chat_template_kwargs"] = chat_template_kwargs

        # Keep Qwen-specific anti-repetition handling centralized so every
        # chat-completion path (streaming and non-streaming) gets the same
        # behavior without duplicating generation code.
        for name, value in self._qwen_sampling_kwargs().items():
            kwargs.setdefault(name, value)

        return self._model.create_chat_completion(**kwargs)

    def unload(self) -> None:
        with self._lock:
            self._unload()

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
        """
        Run inference.
        If stream=True and stream_callback is provided, calls stream_callback
        with each token as it arrives, and returns the full text at the end.

        cancel_check: optional callable polled between tokens (streaming
        only). If it returns True, generation stops immediately and
        whatever was produced so far is returned — this is what makes the
        UI's "Stop" button actually interrupt an in-progress response
        instead of only preventing the *next* task from starting.

        top_k: llama.cpp's own default is 40; passing 0 tells llama.cpp to
        disable top-k filtering entirely (matches its convention of 0 =
        "no limit" for this parameter), so a user setting Top K to 0 in the
        UI really does turn it off rather than silently falling back to 40.
        """
        if not _llama_available and self._runtime_mode != "llama-cli-mtp":
            raise RuntimeError("llama-cpp-python is not installed.")
        if self._model is None:
            raise RuntimeError("No model loaded. Call load_model() first.")

        with self._lock:
            if self._runtime_mode == "llama-cli-mtp":
                return self._mtp_runtime.generate(
                    cli_path=self._llama_cli_path,
                    model_path=self._current_path,
                    mtp_model_path=self._mtp_model_path,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    top_k=top_k,
                    context_size=self._current_n_ctx,
                    gpu_layers=0,
                    threads=4,
                    threads_batch=0,
                    flash_attn=self._mtp_flash_attn,
                    stream=stream,
                    stream_callback=stream_callback,
                    cancel_check=cancel_check,
                )

            if stream and stream_callback:
                return self._stream_generate(
                    messages, max_tokens, temperature, top_p, top_k, stop, stream_callback, cancel_check
                )
            else:
                return self._blocking_generate(
                    messages, max_tokens, temperature, top_p, top_k, stop
                )

    def _blocking_generate(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        stop: Optional[list[str]],
    ) -> str:
        response = self._create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop=stop or [],
            stream=False,
        )
        return response["choices"][0]["message"]["content"] or ""

    def _stream_generate(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        stop: Optional[list[str]],
        stream_callback: Callable[[str], None],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> str:
        import time

        chunks: list[str] = []
        token_count = 0
        t_first: Optional[float] = None
        t_start = time.perf_counter()

        response_iter = self._create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            stop=stop or [],
            stream=True,
        )
        response_iter = iter(response_iter)
        while True:
            if cancel_check is not None and cancel_check():
                logger.info("[llm_engine] Generation cancelled — stopping stream early.")
                break
            try:
                chunk = next(response_iter)
            except StopIteration:
                break
            delta = chunk["choices"][0]["delta"]
            token = delta.get("content", "")
            if token:
                now = time.perf_counter()
                if t_first is None:
                    t_first = now
                chunks.append(token)
                token_count += 1
                stream_callback(token)

        t_end = time.perf_counter()
        total_elapsed = t_end - t_start


        if t_first is not None and token_count > 0:
            decode_elapsed = max(t_end - t_first, 1e-6)
            tok_per_sec = token_count / decode_elapsed
            cancelled = cancel_check is not None and cancel_check()
            status = "cancelado" if cancelled else "completo"
            logger.info(
                "[llm_engine] Generacion %s: %d tokens en %.1fs — "
                "%.1f tok/s (TTFT %.0f ms)",
                status, token_count, total_elapsed,
                tok_per_sec, (t_first - t_start) * 1000,
            )

        return "".join(chunks)



_engine = LLMEngine()


def get_engine() -> LLMEngine:
    return _engine
