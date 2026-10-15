"""
Small JSON-over-HTTP helpers shared by the remote backends (Ollama and
cloud APIs). Standard library only, so no new dependency is needed.

Each backend passes its own exception class, so callers only ever catch
errors from their own backend.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Iterator, Optional, Type

JSON_HEADERS = {"Content-Type": "application/json"}


def _describe_http_error(e: urllib.error.HTTPError, url: str) -> str:
    detail = ""
    try:
        detail = e.read().decode("utf-8", "replace")
    except Exception:
        pass
    detail = detail.strip()[:300]
    return f"HTTP {e.code} from {url}" + (f": {detail}" if detail else "")


def request_json(
    url: str,
    *,
    error_cls: Type[Exception],
    payload: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 10,
) -> dict:
    """GET (no payload) or POST (with payload) and return the decoded JSON body."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {**JSON_HEADERS, **(headers or {})}
    req = urllib.request.Request(
        url,
        data=data,
        headers=req_headers,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as e:
        raise error_cls(_describe_http_error(e, url)) from e
    except (urllib.error.URLError, OSError) as e:
        raise error_cls(f"Could not reach {url} ({e}).") from e

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise error_cls(f"Invalid JSON returned by {url}.") from e


def stream_lines(
    url: str,
    *,
    error_cls: Type[Exception],
    payload: dict,
    headers: Optional[dict] = None,
    timeout: float = 900,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Iterator[str]:
    """
    POST a JSON payload and yield the raw response lines (SSE or NDJSON).
    Stops early (closing the connection) when should_stop() returns True.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={**JSON_HEADERS, **(headers or {})},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise error_cls(_describe_http_error(e, url)) from e
    except (urllib.error.URLError, OSError) as e:
        raise error_cls(f"Could not reach {url} ({e}).") from e

    with resp:
        for raw in resp:
            if should_stop is not None and should_stop():
                return
            line = raw.decode("utf-8", "replace").strip()
            if line:
                yield line
