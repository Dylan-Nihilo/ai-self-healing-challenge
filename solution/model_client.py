"""Minimal OpenAI-compatible chat-completions client.

The runner image has no third-party agent SDK.  Keeping the transport small also
means the model credential is never exposed to the model or to tool logs.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


class ModelConfigurationError(Exception):
    """The supplied environment does not contain a usable model configuration."""


class ModelRequestError(Exception):
    """A model request failed or returned an unsupported response."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float
    max_retries: int

    @classmethod
    def from_env(cls) -> ModelSettings:
        provider = os.environ.get("MODEL_PROVIDER", "openai-compatible").strip().lower()
        api_key = (
            os.environ.get("MODEL_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ).strip()
        model = os.environ.get("MODEL_NAME", "").strip()
        base_url = os.environ.get("MODEL_BASE_URL", "").strip()

        if provider == "deepseek":
            base_url = base_url or "https://api.deepseek.com/v1"
            model = model or "deepseek-chat"
        else:
            base_url = base_url or "https://api.openai.com/v1"

        if not api_key:
            raise ModelConfigurationError("MODEL_API_KEY is not configured")
        if not model:
            raise ModelConfigurationError("MODEL_NAME is not configured")
        if not base_url.startswith(("https://", "http://")):
            raise ModelConfigurationError("MODEL_BASE_URL must be an HTTP(S) URL")

        return cls(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            model=model,
            timeout_seconds=max(5.0, _env_float("MODEL_TIMEOUT_SECONDS", 35.0)),
            max_retries=max(0, min(2, _env_int("MODEL_MAX_RETRIES", 1))),
        )


class OpenAICompatibleModel:
    """Call a Chat Completions-compatible endpoint with OpenAI tool schemas."""

    def __init__(self, settings: ModelSettings):
        self.settings = settings
        # A direct connection is the least surprising default in an isolated
        # runner.  Operators that require an environment proxy can opt in.
        if os.environ.get("MODEL_USE_ENV_PROXY", "").lower() in {"1", "true", "yes"}:
            self.opener = build_opener()
        else:
            self.opener = build_opener(ProxyHandler({}))

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        url = f"{self.settings.base_url}/chat/completions"
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            body,
            {
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "self-healing-workstation/1.0",
            },
            method="POST",
        )

        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                with self.opener.open(request, timeout=self.settings.timeout_seconds) as response:
                    raw = response.read()
                parsed = json.loads(raw)
                message = parsed["choices"][0]["message"]
                if not isinstance(message, dict):
                    raise ModelRequestError("model_bad_response", "Model returned an invalid message")
                return message
            except HTTPError as error:
                status = error.code
                last_error = ModelRequestError(
                    f"model_http_{status}", f"Model API returned HTTP {status}"
                )
                retryable = status == 429 or 500 <= status <= 504
                if not retryable or attempt >= self.settings.max_retries:
                    raise last_error from error
            except (URLError, TimeoutError, OSError) as error:
                last_error = ModelRequestError(
                    "model_unreachable", "Model API could not be reached"
                )
                if attempt >= self.settings.max_retries:
                    raise last_error from error
            except (KeyError, IndexError, TypeError, ValueError) as error:
                raise ModelRequestError(
                    "model_bad_response", "Model API returned an unsupported response"
                ) from error

            time.sleep(min(2.0, 0.4 * (2**attempt)))

        raise ModelRequestError("model_unreachable", f"Model request failed: {last_error}")
