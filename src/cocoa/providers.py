from __future__ import annotations

import json
import os
import subprocess
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


@dataclass(frozen=True)
class CodexConfig:
    binary: str = "codex"
    model: str | None = None
    timeout_seconds: float = 60.0
    sandbox: str = "read-only"
    ask_for_approval: str = "never"
    codex_home: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "CodexConfig":
        provider = env.get("COCOA_PROVIDER", "stub").lower()
        if provider != "codex":
            raise ProviderConfigurationError(f"unsupported provider: {provider}")

        binary = env.get("COCOA_CODEX_BINARY", "codex")
        model = env.get("COCOA_CODEX_MODEL")
        if not binary:
            binary = "codex"

        timeout_raw = env.get("COCOA_CODEX_TIMEOUT_SECONDS")
        sandbox = env.get("COCOA_CODEX_SANDBOX", "read-only")
        ask_for_approval = env.get("COCOA_CODEX_APPROVAL", "never")
        codex_home = env.get("COCOA_CODEX_HOME") or env.get("CODEX_HOME")

        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ProviderConfigurationError(
                "COCOA_CODEX_SANDBOX must be read-only, workspace-write, or danger-full-access"
            )
        if ask_for_approval not in {"untrusted", "on-failure", "on-request", "never"}:
            raise ProviderConfigurationError(
                "COCOA_CODEX_APPROVAL must be untrusted, on-failure, on-request, or never"
            )

        return cls(
            binary=binary,
            model=model,
            timeout_seconds=float(timeout_raw) if timeout_raw else cls.timeout_seconds,
            sandbox=sandbox,
            ask_for_approval=ask_for_approval,
            codex_home=codex_home,
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


class CodexProvider:
    def __init__(self, config: CodexConfig) -> None:
        self.config = config

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return self._complete_sync(request)

    def _complete_sync(self, request: ProviderRequest) -> ProviderResponse:
        command = self._build_command(request)
        env = dict(os.environ)
        if self.config.codex_home:
            env["CODEX_HOME"] = self.config.codex_home

        try:
            completed = subprocess.run(
                command,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ProviderConfigurationError(
                f'codex executable not found: {self.config.binary}. Set COCOA_CODEX_BINARY to a valid path.'
            ) from exc
        except OSError as exc:
            raise ProviderResponseError(f"failed to run codex: {exc}") from exc

        message, event_error = self._extract_agent_message(completed.stdout)
        if completed.returncode != 0:
            error_message = event_error or self._extract_plain_error(completed.stderr, completed.stdout)
            raise ProviderResponseError(
                f"codex exec failed with code {completed.returncode}: {error_message}"
            )

        if message is None:
            if event_error:
                raise ProviderResponseError(event_error)
            raise ProviderResponseError("codex returned empty response")

        return ProviderResponse(message=message, summary=message[:80])

    def _build_command(self, request: ProviderRequest) -> list[str]:
        args: list[str] = [
            self.config.binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--ask-for-approval",
            self.config.ask_for_approval,
            "--sandbox",
            self.config.sandbox,
            "--cd",
            request.cwd,
        ]
        if self.config.model:
            args.extend(["-m", self.config.model])
        args.append(self._format_prompt(request))
        return args

    def _format_prompt(self, request: ProviderRequest) -> str:
        return "\n".join(
            [
                f"Workspace: {request.cwd}",
                f"Thread: {request.thread_id}",
                f"Turn: {request.turn_id}",
                "",
                request.prompt,
            ]
        )

    def _extract_agent_message(self, raw: str) -> tuple[str | None, str | None]:
        last_message: str | None = None
        last_error: str | None = None
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "item.completed":
                item = event.get("item")
                text = self._extract_item_text(item)
                if text is not None:
                    last_message = text
            elif event_type == "error":
                message = event.get("message")
                if isinstance(message, str):
                    last_error = message
            elif event_type == "turn.failed":
                error = event.get("error")
                if isinstance(error, dict):
                    message = error.get("message")
                    if isinstance(message, str):
                        last_error = message
        return last_message, last_error

    def _extract_item_text(self, item: object) -> str | None:
        if not isinstance(item, dict):
            return None
        details = item.get("details")
        if not isinstance(details, dict):
            return None
        if details.get("type") != "agent_message":
            return None
        text = details.get("text")
        if isinstance(text, str) and text.strip():
            return text
        return None

    def _extract_plain_error(self, stderr: str, stdout: str) -> str:
        for source in [stderr, stdout]:
            for raw in source.splitlines()[::-1]:
                text = raw.strip()
                if text:
                    return text
        return "(empty output)"


def provider_from_env(env: Mapping[str, str] = os.environ) -> ProviderAdapter:
    provider = env.get("COCOA_PROVIDER", "stub").lower()
    if provider in {"", "stub"}:
        return StubProvider()
    if provider in {"openai", "openai-compatible"}:
        config = OpenAICompatibleConfig.from_env(env)
        if config is None:
            raise ProviderConfigurationError("missing OpenAI configuration")
        return OpenAICompatibleProvider(config)
    if provider == "codex":
        return CodexProvider(CodexConfig.from_env(env))
    raise ProviderConfigurationError(f"unsupported provider: {provider}")


def provider_name_from_env(env: Mapping[str, str] = os.environ) -> str:
    provider = env.get("COCOA_PROVIDER", "stub").lower()
    if provider in {"", "stub"}:
        return "stub"
    if provider in {"openai", "openai-compatible"}:
        config = OpenAICompatibleConfig.from_env(env)
        if config is None:
            raise ProviderConfigurationError("missing OpenAI configuration")
        return f"openai-compatible:{config.model}"
    if provider == "codex":
        config = CodexConfig.from_env(env)
        if config.model:
            return f"codex:{config.model}"
        return "codex"
    raise ProviderConfigurationError(f"unsupported provider: {provider}")


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
