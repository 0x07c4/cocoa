from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from typing import Mapping
from typing import Protocol


@dataclass(frozen=True)
class ProviderRequest:
    thread_id: str
    turn_id: str
    prompt: str
    cwd: str


@dataclass(frozen=True)
class ProviderResponse:
    message: str
    summary: str | None = None


class ProviderError(RuntimeError):
    pass


class ProviderConfigurationError(ProviderError):
    pass


class ProviderHTTPError(ProviderError):
    pass


class ProviderResponseError(ProviderError):
    pass


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 60.0
    temperature: float | None = None
    max_tokens: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "OpenAICompatibleConfig | None":
        provider = env.get("COCOA_PROVIDER", "stub").lower()
        if provider in {"", "stub"}:
            return None
        if provider not in {"openai", "openai-compatible"}:
            raise ProviderConfigurationError(f"unsupported provider: {provider}")

        api_key = env.get("COCOA_OPENAI_API_KEY") or env.get("OPENAI_API_KEY")
        model = env.get("COCOA_OPENAI_MODEL") or env.get("OPENAI_MODEL")
        if not api_key or not model:
            missing = []
            if not api_key:
                missing.append("COCOA_OPENAI_API_KEY or OPENAI_API_KEY")
            if not model:
                missing.append("COCOA_OPENAI_MODEL or OPENAI_MODEL")
            raise ProviderConfigurationError("missing " + ", ".join(missing))

        base_url = env.get("COCOA_OPENAI_BASE_URL") or env.get("OPENAI_BASE_URL")
        timeout_raw = env.get("COCOA_OPENAI_TIMEOUT_SECONDS")
        temperature_raw = env.get("COCOA_OPENAI_TEMPERATURE")
        max_tokens_raw = env.get("COCOA_OPENAI_MAX_TOKENS")
        return cls(
            api_key=api_key,
            model=model,
            base_url=(base_url or cls.base_url).rstrip("/"),
            timeout_seconds=float(timeout_raw) if timeout_raw else cls.timeout_seconds,
            temperature=float(temperature_raw) if temperature_raw else None,
            max_tokens=int(max_tokens_raw) if max_tokens_raw else None,
        )


class ProviderAdapter(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError


class OpenAICompatibleProvider:
    def __init__(self, config: OpenAICompatibleConfig) -> None:
        self.config = config

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return self._complete_sync(request)

    def _complete_sync(self, request: ProviderRequest) -> ProviderResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "developer",
                    "content": (
                        "You are cocoa, a terminal-native coding assistant. "
                        "Return concise, actionable responses. Do not claim to have "
                        "changed files or run commands unless the cocoa runtime did it."
                    ),
                },
                {
                    "role": "user",
                    "content": self._format_user_prompt(request),
                },
            ],
        }
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if self.config.max_tokens is not None:
            payload["max_tokens"] = self.config.max_tokens

        body = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.config.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                http_request,
                timeout=self.config.timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise ProviderHTTPError(
                f"provider HTTP {exc.code}: {self._extract_error_message(error_body)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderHTTPError(f"provider connection failed: {exc.reason}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError("provider returned invalid JSON") from exc

        return ProviderResponse(
            message=self._extract_message(data),
        )

    def _format_user_prompt(self, request: ProviderRequest) -> str:
        return "\n".join(
            [
                f"Workspace: {request.cwd}",
                f"Thread: {request.thread_id}",
                f"Turn: {request.turn_id}",
                "",
                request.prompt,
            ]
        )

    def _extract_message(self, data: dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderResponseError("provider response missing choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise ProviderResponseError("provider response has invalid choice")
        message = first.get("message")
        if not isinstance(message, dict):
            raise ProviderResponseError("provider response missing message")
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "\n".join(parts)
        raise ProviderResponseError("provider response missing text content")

    def _extract_error_message(self, body: str) -> str:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return body[:500] or "(empty error body)"
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        return body[:500] or "(empty error body)"


def provider_from_env(env: Mapping[str, str] = os.environ) -> ProviderAdapter:
    config = OpenAICompatibleConfig.from_env(env)
    if config is None:
        return StubProvider()
    return OpenAICompatibleProvider(config)


def provider_name_from_env(env: Mapping[str, str] = os.environ) -> str:
    config = OpenAICompatibleConfig.from_env(env)
    if config is None:
        return "stub"
    return f"openai-compatible:{config.model}"


class StubProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        lines = [
            "Provider is not configured yet.",
            "This turn has been recorded by cocoa's runtime.",
            "Next step: connect an OpenAI-compatible provider adapter.",
        ]
        return ProviderResponse(
            message="\n".join(lines),
            summary=f"stub response for: {request.prompt[:80]}",
        )
