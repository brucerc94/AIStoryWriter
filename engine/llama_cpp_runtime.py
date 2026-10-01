"""Native llama.cpp CLI runtime used for model-level MTP generation.

MTP itself is executed by llama.cpp's native llama-cli executable. The
application does not start llama-server and does not duplicate model-loading
logic: normal models continue through the existing llama-cpp-python path,
while a model with an MTP profile is delegated to llama-cli with the native
draft-mtp options.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class LlamaCppCliRuntime:
    """Run one native llama-cli MTP inference at a time."""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    @staticmethod
    def build_command(
        cli_path: str,
        model_path: str,
        mtp_model_path: str,
        context_size: int,
        gpu_layers: int,
        threads: int,
        threads_batch: int,
        temperature: float,
        top_p: float,
        top_k: int,
        max_tokens: int,
        flash_attn: bool,
        system_prompt_file: str,
        prompt_file: str,
    ) -> list[str]:
        """Build the native llama.cpp command line for one MTP generation."""
        command = [
            cli_path,
            "--model", model_path,
            "--model-draft", mtp_model_path,
            "--spec-type", "draft-mtp",
            "--spec-draft-n-max", "3",
            "--ctx-size", str(context_size),
            "--n-gpu-layers", str(gpu_layers),
            "--threads", str(threads),
            "--batch-size", "2048",
            "--ubatch-size", "512",
            "--flash-attn", "on" if flash_attn else "off",
            "--temp", str(temperature),
            "--top-p", str(top_p),
            "--top-k", str(top_k),
            "--predict", str(max_tokens),
            "--jinja",
            "--single-turn",
            "--conversation",
            "--system-prompt-file", system_prompt_file,
            "--file", prompt_file,
            "--no-display-prompt",
            "--no-show-timings",
            "--log-disable",
            "--simple-io",
            "--color", "off",
            "--no-escape",
        ]

        if threads_batch > 0:
            command.extend(["--threads-batch", str(threads_batch)])

        return command

    def generate(
        self,
        cli_path: str,
        model_path: str,
        mtp_model_path: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        context_size: int,
        gpu_layers: int,
        threads: int,
        threads_batch: int = 0,
        flash_attn: bool = False,
        stream: bool = False,
        stream_callback: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> str:
        if not os.path.isfile(cli_path):
            raise RuntimeError(f"llama-cli executable not found: {cli_path}")
        if not os.path.isfile(model_path):
            raise RuntimeError(f"Target GGUF not found: {model_path}")
        if not os.path.isfile(mtp_model_path):
            raise RuntimeError(f"MTP GGUF not found: {mtp_model_path}")
        if not messages:
            raise RuntimeError("No messages supplied for llama.cpp inference.")

        system_parts = [
            str(m.get("content", "")).strip()
            for m in messages
            if str(m.get("role", "")).lower() == "system" and str(m.get("content", "")).strip()
        ]
        system_prompt = "\n\n".join(system_parts)

        non_system = [
            (str(m.get("role", "")).lower(), str(m.get("content", "")))
            for m in messages
            if str(m.get("role", "")).lower() != "system"
        ]

        if not non_system:
            user_prompt = ""
        elif len(non_system) == 1 and non_system[0][0] == "user":
            user_prompt = non_system[0][1]
        else:
            history = []
            for role, content in non_system[:-1]:
                if not content.strip():
                    continue
                label = "User" if role == "user" else "Assistant"
                history.append(f"{label}:\n{content.strip()}")

            _, current_content = non_system[-1]
            if history:
                user_prompt = (
                    "Conversation history:\n\n"
                    + "\n\n---\n\n".join(history)
                    + "\n\n---\n\nCurrent user request:\n"
                    + current_content
                )
            else:
                user_prompt = current_content

        with tempfile.TemporaryDirectory(prefix="aistory_mtp_") as temp_dir:
            root = Path(temp_dir)
            system_file = root / "system.txt"
            prompt_file = root / "prompt.txt"
            system_file.write_text(system_prompt, encoding="utf-8")
            prompt_file.write_text(user_prompt, encoding="utf-8")

            command = self.build_command(
                cli_path=cli_path,
                model_path=model_path,
                mtp_model_path=mtp_model_path,
                context_size=context_size,
                gpu_layers=gpu_layers,
                threads=threads,
                threads_batch=threads_batch,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                max_tokens=max_tokens,
                flash_attn=flash_attn,
                system_prompt_file=str(system_file),
                prompt_file=str(prompt_file),
            )

            env = os.environ.copy()
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
            with self._lock:
                self._process = process

            chunks: list[str] = []
            stderr_chunks: list[str] = []
            cancelled = False

            def drain_stderr() -> None:
                if process.stderr is None:
                    return
                for line in process.stderr:
                    stderr_chunks.append(line)

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stderr_thread.start()

            try:
                if process.stdout is None:
                    raise RuntimeError("llama-cli stdout pipe was not created.")

                while True:
                    if cancel_check is not None and cancel_check():
                        cancelled = True
                        self.stop()
                        break

                    char = process.stdout.read(1)
                    if char:
                        cleaned = strip_ansi(char)
                        if cleaned:
                            chunks.append(cleaned)
                            if stream and stream_callback:
                                stream_callback(cleaned)
                        continue

                    if process.poll() is not None:
                        break

                if process.poll() is None:
                    process.wait(timeout=2)

                stderr_thread.join(timeout=1)
                return_code = process.returncode
                stderr_text = strip_ansi("".join(stderr_chunks)).strip()

                result = "".join(chunks).strip()
                if cancelled:
                    return result
                if return_code not in (0, None):
                    detail = stderr_text[-4000:] if stderr_text else "llama-cli exited with an error."
                    raise RuntimeError(
                        f"llama-cli failed with exit code {return_code}:\n{detail}"
                    )
                if not result:
                    if stderr_text:
                        raise RuntimeError(
                            "llama-cli returned no text.\n" + stderr_text[-4000:]
                        )
                    return ""
                return result
            finally:
                with self._lock:
                    if self._process is process:
                        self._process = None
                if process.poll() is None:
                    try:
                        process.kill()
                    except Exception:
                        pass
