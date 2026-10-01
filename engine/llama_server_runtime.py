"""
Optional llama.cpp server runtime for model-specific speculative decoding.

The rest of AI Story Studio keeps using LLMEngine.generate().  This helper
only owns the process/HTTP transport needed when a model has an MTP profile.
When no MTP profile is configured, the normal llama-cpp-python path remains
unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

logger = logging.getLogger("llama_server_runtime")


class LlamaServerRuntime:
    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._base_url: str = ""
        self._model_id: str = ""
        self._model_path: str = ""
        self._draft_path: str = ""
        self._context_size: int = 0
        self._stderr_lines: list[str] = []
        self._output_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def context_size(self) -> int:
        return self._context_size

    @property
    def model_path(self) -> str:
        return self._model_path

    @property
    def draft_path(self) -> str:
        return self._draft_path

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _request_json(
        url: str,
        payload: Optional[dict] = None,
        timeout: float = 10.0,
    ) -> dict:
        data = None
        headers = {}
        method = "GET"
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw)

    def start(
        self,
        *,
        server_path: str,
        model_path: str,
        draft_model_path: str,
        context_size: int,
        gpu_layers: int,
        threads: int,
        threads_batch: int,
        batch_size: int = 1024,
        ubatch_size: int = 1024,
        flash_attn: bool = False,
        extra_env: Optional[dict[str, str]] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        with self._lock:
            if self.running and (
                self._model_path == model_path
                and self._draft_path == draft_model_path
                and self._context_size == context_size
            ):
                return

            self.stop()

            if not os.path.isfile(server_path):
                raise RuntimeError(f"llama-server executable not found: {server_path}")
            if not os.path.isfile(model_path):
                raise RuntimeError(f"Model file not found: {model_path}")
            if not os.path.isfile(draft_model_path):
                raise RuntimeError(f"MTP draft model file not found: {draft_model_path}")

            port = self._free_port()
            args = [
                os.path.abspath(server_path),
                "--model", os.path.abspath(model_path),
                "--model-draft", os.path.abspath(draft_model_path),
                "--spec-type", "draft-mtp",
                "--host", "127.0.0.1",
                "--port", str(port),
                "--ctx-size", str(context_size),
                "--n-gpu-layers", str(gpu_layers),
                "--threads", str(threads),
                "--parallel", "1",
                "--jinja",
            ]

            if threads_batch > 0:
                args += ["--threads-batch", str(threads_batch)]
            if batch_size > 0:
                args += ["--batch-size", str(batch_size)]
            if ubatch_size > 0:
                args += ["--ubatch-size", str(ubatch_size)]
            args += ["--flash-attn", "on" if flash_attn else "off"]

            env = os.environ.copy()
            server_dir = os.path.dirname(os.path.abspath(server_path))
            env["PATH"] = server_dir + os.pathsep + env.get("PATH", "")
            if extra_env:
                env.update(extra_env)

            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                self._process = subprocess.Popen(
                    args,
                    cwd=server_dir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    creationflags=flags,
                )
            except Exception as exc:
                self._process = None
                raise RuntimeError(f"Failed to start llama-server: {exc}") from exc

            self._stderr_lines = []
            self._output_thread = threading.Thread(
                target=self._drain_output,
                name="llama-server-reader",
                daemon=True,
            )
            self._output_thread.start()

            self._base_url = f"http://127.0.0.1:{port}"
            self._model_id = os.path.basename(model_path)
            self._model_path = os.path.abspath(model_path)
            self._draft_path = os.path.abspath(draft_model_path)
            self._context_size = int(context_size)

            if progress_callback:
                progress_callback(
                    f"Starting llama.cpp MTP runtime: {os.path.basename(draft_model_path)}"
                )

            deadline = time.monotonic() + 300.0
            health_url = self._base_url + "/health"
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    tail = "\n".join(self._stderr_lines[-20:])
                    self.stop()
                    raise RuntimeError(
                        "llama-server exited while loading the MTP model."
                        + (f"\n{tail}" if tail else "")
                    )

                try:
                    with urllib.request.urlopen(health_url, timeout=2.0) as response:
                        if int(response.status) == 200:
                            if progress_callback:
                                progress_callback("llama.cpp MTP runtime ready.")
                            return
                except (urllib.error.URLError, TimeoutError, OSError):
                    pass

                if progress_callback:
                    progress_callback("Loading model + MTP head…")
                time.sleep(0.5)

            self.stop()
            raise RuntimeError("Timed out waiting for llama-server to become ready.")

    def _drain_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return

        try:
            for raw_line in process.stdout:
                line = raw_line.rstrip()
                if not line:
                    continue
                self._stderr_lines.append(line)
                if len(self._stderr_lines) > 200:
                    del self._stderr_lines[:-200]
                logger.debug("[llama-server] %s", line)
        except Exception as exc:
            logger.debug("[llama-server] output reader stopped: %s", exc)

    def generate(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        stop: Optional[list[str]] = None,
        stream: bool = False,
        stream_callback: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> str:
        if not self.running:
            raise RuntimeError("llama-server MTP runtime is not running.")

        payload = {
            "model": self._model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "stop": stop or [],
            "stream": bool(stream and stream_callback),
        }

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._base_url + "/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        response = None
        try:
            response = urllib.request.urlopen(request, timeout=None)
            if payload["stream"]:
                chunks: list[str] = []
                while True:
                    if cancel_check is not None and cancel_check():
                        logger.info("[llama-server] Generation cancelled by caller.")
                        break

                    raw = response.readline()
                    if not raw:
                        break

                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue

                    data_line = line[5:].strip()
                    if data_line == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_line)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    token = delta.get("content") or ""
                    if token:
                        chunks.append(token)
                        stream_callback(token)

                return "".join(chunks)

            raw = response.read().decode("utf-8")
            result = json.loads(raw)
            choices = result.get("choices") or []
            if not choices:
                raise RuntimeError(f"llama-server returned no choices: {result}")
            return (choices[0].get("message") or {}).get("content") or ""
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(
                f"llama-server HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def stop(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except Exception:
                        pass

            self._base_url = ""
            self._model_id = ""
            self._model_path = ""
            self._draft_path = ""
            self._context_size = 0
            self._output_thread = None
