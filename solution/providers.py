"""Minimal zero-dependency DeepSeek client used by the repair agent."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class ProviderError(RuntimeError):
    """A model request failed or returned an unexpected response."""


def _setting(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        raise ProviderError(f"missing model setting: {name}")
    return value


def chat(messages: list[dict[str, str]], *, timeout: float = 45.0) -> str:
    """Send one chat request to DeepSeek and return the assistant text."""
    api_key = _setting("MODEL_API_KEY", "")
    base_url = _setting("MODEL_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = _setting("MODEL_NAME", "deepseek-chat")
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ProviderError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError(f"DeepSeek connection failed: {exc}") from exc

    try:
        body = json.loads(raw)
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProviderError("DeepSeek returned an invalid chat response") from exc
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("DeepSeek returned empty assistant content")
    return content
