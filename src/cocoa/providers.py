from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request
import time
from dataclasses import dataclass
from typing import Any
from typing import Mapping
from typing import Protocol


_CODEX_JWT_REFRESH_SKEW_SECONDS = 120
_CODEX_DEFAULT_MODELS = [
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
]

COCOA_DEVELOPER_INSTRUCTIONS = (
    "You are cocoa, a terminal-native coding assistant. "
    "Return concise, actionable responses. Do not claim to have changed files "
    "or run commands unless the cocoa runtime did it. When a shell command or file write "
    "would help, propose it instead of claiming to do it by appending a fenced block "
    "whose opening fence is exactly ```cocoa-proposal and whose JSON has this shape: "
    "{\"commands\":[{\"command\":\"...\",\"reason\":\"...\"}],"
    "\"write_files\":[{\"path\":\"...\",\"content\":\"...\",\"reason\":\"...\"}]}. "
    "The user decides whether cocoa executes proposed commands or applies file writes."
)


def _resolve_codex_models(
    api_key: str, base_url: str, *, timeout_seconds: float = 8.0
) -> list[str]:
    endpoint = f"{base_url.rstrip('/')}/models?client_version=1.0.0"
    request = urllib.request.Request(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return []
    except OSError:
        return []

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []

    visible: list[tuple[int, str]] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str):
            continue
        slug = slug.strip()
        if not slug:
            continue

        visibility = item.get("visibility")
        if visibility in {"hide", "hidden"}:
            continue
        if item.get("supported_in_api") is False:
            continue

        priority = item.get("priority")
        rank = 10_000
        if isinstance(priority, (int, float)):
            rank = int(priority)
        visible.append((rank, slug))

    if not visible:
        return []
    visible.sort(key=lambda item: (item[0], item[1]))
    return [slug for _, slug in visible]


def _resolve_codex_model(env: Mapping[str, str], api_key: str, base_url: str) -> str:
    explicit_model = env.get("COCOA_CODEX_MODEL") or env.get("OPENAI_MODEL")
    if explicit_model:
        return explicit_model.strip()

    discovered = _resolve_codex_models(api_key, base_url)
    if discovered:
        return discovered[0]

    return _CODEX_DEFAULT_MODELS[0]


def _codex_home_from_env(env: Mapping[str, str]) -> Path:
    raw = (
        env.get("COCOA_CODEX_HOME")
        or env.get("CODEX_HOME")
        or str(Path.home() / ".codex")
    )
    raw = str(raw).strip()
    return Path(raw).expanduser()


def _read_jwt_expiry_seconds(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = base64.urlsafe_b64decode(
            parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        )
        claims = json.loads(payload.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    try:
        return float(exp)
    except (TypeError, ValueError):
        return None


def _is_jwt_expired(token: str, *, skew_seconds: int = 0) -> bool:
    exp = _read_jwt_expiry_seconds(token)
    if exp is None:
        return False
    return exp <= (time.time() + max(0, int(skew_seconds)))


def _read_codex_auth_token(codex_home: Path) -> str | None:
    auth_path = codex_home / "auth.json"
    if not auth_path.is_file():
        return None
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return None
    cleaned = access_token.strip()
    if _is_jwt_expired(cleaned, skew_seconds=_CODEX_JWT_REFRESH_SKEW_SECONDS):
        return None
    return cleaned


def _resolve_codex_api_key(env: Mapping[str, str]) -> str | None:
    codex_home = _codex_home_from_env(env)
    api_key = (
        env.get("COCOA_CODEX_API_KEY")
        or env.get("OPENAI_API_KEY")
        or env.get("OPENAI_TOKEN")
        or env.get("CODEX_API_KEY")
    )
    if api_key:
        cleaned = str(api_key).strip()
        if cleaned:
            return cleaned
    return _read_codex_auth_token(codex_home)


def _resolve_provider_from_env(env: Mapping[str, str]) -> str:
    explicit = env.get("COCOA_PROVIDER", "").strip().lower()
    if explicit:
        return explicit

    if (env.get("COCOA_OPENAI_API_KEY") or env.get("OPENAI_API_KEY")) and (
        env.get("COCOA_OPENAI_MODEL") or env.get("OPENAI_MODEL")
    ):
        return "openai"

    explicit_codex_key = env.get("COCOA_CODEX_API_KEY") or env.get("CODEX_API_KEY")
    if explicit_codex_key:
        return "codex-http"
    if _read_codex_auth_token(_codex_home_from_env(env)):
        return "codex-http"

    return "stub"


@dataclass(frozen=True)
class ProviderRequest:
    thread_id: str
    turn_id: str
    prompt: str
    cwd: str
    thread_context: str | None = None
    workspace_context: str | None = None


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
    def from_env(
        cls,
        env: Mapping[str, str] = os.environ,
        provider: str | None = None,
    ) -> "OpenAICompatibleConfig | None":
        provider = (provider if provider is not None else env.get("COCOA_PROVIDER", "stub")).lower()
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
class CodexResponsesConfig:
    api_key: str
    model: str
    base_url: str = "https://chatgpt.com/backend-api/codex"
    timeout_seconds: float = 60.0
    temperature: float | None = None
    max_tokens: int | None = None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] = os.environ,
        provider: str | None = None,
    ) -> "CodexResponsesConfig | None":
        provider = (provider if provider is not None else env.get("COCOA_PROVIDER", "stub")).lower()
        if provider not in {"codex-http", "codex-responses", "openai-codex"}:
            return None

        api_key = _resolve_codex_api_key(env)
        base_url = env.get("COCOA_CODEX_BASE_URL") or env.get("OPENAI_BASE_URL")
        model = _resolve_codex_model(env, api_key=api_key, base_url=(base_url or cls.base_url).rstrip("/"))
        if not api_key or not model:
            missing = []
            if not api_key:
                codex_home = _codex_home_from_env(env)
                auth_path = codex_home / "auth.json"
                missing.append(
                    "COCOA_CODEX_API_KEY or OPENAI_API_KEY or OPENAI_TOKEN or CODEX_API_KEY "
                    f"or a valid token in {auth_path}"
                )
            raise ProviderConfigurationError("missing " + ", ".join(missing))

        timeout_raw = env.get("COCOA_CODEX_TIMEOUT_SECONDS")
        temperature_raw = env.get("COCOA_CODEX_TEMPERATURE")
        max_tokens_raw = env.get("COCOA_CODEX_MAX_TOKENS")

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
    def from_env(
        cls,
        env: Mapping[str, str] = os.environ,
        provider: str | None = None,
    ) -> "CodexConfig":
        provider = (provider if provider is not None else env.get("COCOA_PROVIDER", "stub")).lower()
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
                    "content": COCOA_DEVELOPER_INSTRUCTIONS,
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
        lines = [
            f"Workspace: {request.cwd}",
            f"Thread: {request.thread_id}",
            f"Turn: {request.turn_id}",
            "",
        ]
        if request.thread_context:
            lines.append("Recent thread context:")
            lines.append(request.thread_context)
            lines.append("")
        if request.workspace_context:
            lines.append("Workspace context:")
            lines.append(request.workspace_context)
            lines.append("")
        lines.append(request.prompt)
        return "\n".join(lines)

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
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"]
        return body[:500] or "(empty error body)"


class CodexResponsesProvider:
    def __init__(self, config: CodexResponsesConfig) -> None:
        self.config = config

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return self._complete_sync(request)

    def _complete_sync(self, request: ProviderRequest) -> ProviderResponse:
        payload = self._build_responses_payload(request, as_list=False)
        try:
            raw = self._send_responses_request(payload)
        except ProviderHTTPError as exc:
            payload_list = self._build_responses_payload(request, as_list=True)
            if (
                "Input must be a list" in str(exc)
                and payload["input"] != payload_list["input"]
            ):
                raw = self._send_responses_request(payload_list)
            else:
                raise
        except urllib.error.URLError as exc:
            raise ProviderHTTPError(f"provider connection failed: {exc.reason}") from exc

        stream_events = None
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                try:
                    message = self._extract_message(data)
                except ProviderResponseError:
                    stream_events = self._parse_responses_stream(raw)
                    message = self._extract_message_from_stream_events(stream_events)
            else:
                stream_events = self._parse_responses_stream(raw)
                message = self._extract_message_from_stream_events(stream_events)
        except json.JSONDecodeError as exc:
            if stream_events is None:
                stream_events = self._parse_responses_stream(raw)
            message = self._extract_message_from_stream_events(stream_events)
            if not isinstance(message, str):
                raise ProviderResponseError("provider response missing text content") from exc

        if not isinstance(message, str):
            raise ProviderResponseError("provider response missing text content")
        if not message.strip():
            raise ProviderResponseError("provider returned empty response")
        return ProviderResponse(message=message, summary=message[:80])

    def _build_responses_payload(
        self, request: ProviderRequest, *, as_list: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "input": self._format_user_prompt(request)
            if not as_list
            else self._build_responses_input_list(request),
            "instructions": COCOA_DEVELOPER_INSTRUCTIONS,
        }
        payload["store"] = False
        payload["stream"] = True
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if self.config.max_tokens is not None:
            payload["max_output_tokens"] = self.config.max_tokens
        return payload

    def _parse_responses_stream(self, raw: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in raw.splitlines():
            text = line.strip()
            if not text or text.startswith(":"):
                continue
            if text.startswith("data:"):
                text = text[5:].strip()
                if text == "[DONE]" or not text:
                    continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
            elif isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        events.append(item)
        return events

    def _extract_message_from_stream_events(self, events: list[dict[str, Any]]) -> str | None:
        if not events:
            return None

        text_parts: list[str] = []
        collected_output_items: list[dict[str, Any]] = []
        terminal_response: dict[str, Any] | None = None

        for event in events:
            event_type = event.get("type")
            if event_type == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict):
                    collected_output_items.append(item)
            elif event_type in {"response.output_text", "response.output_text.delta", "response.output_text.done"}:
                delta = event.get("delta")
                if not isinstance(delta, str):
                    delta = event.get("text")
                if isinstance(delta, str):
                    text_parts.append(delta)

            if event_type in {"response.completed", "response.incomplete", "response.failed"}:
                response = event.get("response")
                if isinstance(response, dict):
                    terminal_response = response

            if terminal_response is None and isinstance(event.get("response"), dict):
                terminal_response = event["response"]

        if terminal_response is not None:
            try:
                return self._extract_message(terminal_response)
            except ProviderResponseError:
                output = terminal_response.get("output")
                if isinstance(output, list):
                    text = self._extract_output_text(output)
                    if text:
                        return text

        if collected_output_items:
            assembled_output = self._extract_output_text(collected_output_items)
            if assembled_output:
                return assembled_output

        if text_parts:
            return "".join(text_parts).strip()
        return None

    def _build_responses_input_list(self, request: ProviderRequest) -> list[dict[str, Any]]:
        return [{"role": "user", "content": self._format_user_prompt(request)}]

    def _send_responses_request(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.config.base_url.rstrip('/')}/responses",
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
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise ProviderHTTPError(
                f"provider HTTP {exc.code}: {self._extract_error_message(error_body)}"
            ) from exc

    def _format_user_prompt(self, request: ProviderRequest) -> str:
        return (
            f"Workspace: {request.cwd}\n"
            f"Thread: {request.thread_id}\n"
            f"Turn: {request.turn_id}\n\n"
            + (
                f"Recent thread context:\n{request.thread_context}\n\n"
                if request.thread_context
                else ""
            )
            + (
                f"Workspace context:\n{request.workspace_context}\n\n"
                if request.workspace_context
                else ""
            )
            + request.prompt
        )

    def _extract_message(self, data: dict[str, Any]) -> str:
        if self._response_has_failure(data):
            raise ProviderResponseError(self._extract_error_message(json.dumps(data)))

        output = data.get("output")
        if isinstance(output, list):
            text = self._extract_output_text(output)
            if text:
                return text

        if isinstance(data.get("output_text"), str):
            out = data.get("output_text").strip()
            if out:
                return out

        if self._is_chat_completion_style(data):
            return self._extract_chat_completion_like_message(data)

        raise ProviderResponseError("provider response missing text content")

    def _response_has_failure(self, data: dict[str, Any]) -> bool:
        status = data.get("status")
        if isinstance(status, str) and status.strip().lower() in {"failed", "cancelled", "error"}:
            return True
        error_obj = data.get("error")
        if isinstance(error_obj, dict) and error_obj.get("message"):
            return True
        return False

    def _extract_output_text(self, output: list[Any]) -> str:
        pieces: list[str] = []
        for raw_item in output:
            if not isinstance(raw_item, dict):
                continue
            item_type = raw_item.get("type")
            if item_type == "output_text":
                text = raw_item.get("text")
                if isinstance(text, str) and text.strip():
                    pieces.append(text)
                continue
            if item_type == "message":
                message_content = raw_item.get("content")
                if isinstance(message_content, list):
                    for raw_part in message_content:
                        if not isinstance(raw_part, dict):
                            continue
                        part_type = raw_part.get("type")
                        if part_type not in {"output_text", "text"}:
                            continue
                        text = raw_part.get("text")
                        if isinstance(text, str) and text.strip():
                            pieces.append(text)
        return "".join(pieces).strip()

    def _is_chat_completion_style(self, data: dict[str, Any]) -> bool:
        choices = data.get("choices")
        if isinstance(choices, list):
            return True
        message = data.get("message")
        if isinstance(message, dict) and message.get("content"):
            return True
        return False

    def _extract_chat_completion_like_message(self, data: dict[str, Any]) -> str:
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
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return body[:500] or "(empty error body)"
        if isinstance(parsed, dict):
            if isinstance(parsed.get("error"), dict):
                error = parsed["error"]
                if isinstance(error.get("message"), str):
                    return error["message"]
            message = parsed.get("message")
            if isinstance(message, str):
                return message
        return body[:500] or "(empty error body)"


class CodexProvider:
    def __init__(self, config: CodexConfig) -> None:
        self.config = config
        self._supports_ask_for_approval: bool | None = None

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return self._complete_sync(request)

    def _complete_sync(self, request: ProviderRequest) -> ProviderResponse:
        command = self._build_command(request)
        env = dict(os.environ)
        if self.config.codex_home:
            env["CODEX_HOME"] = self.config.codex_home

        try:
            completed = self._run_codex(command, env=env, timeout=self.config.timeout_seconds)
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

    def _run_codex(self, command: list[str], *, env: dict[str, str], timeout: float):
        completed = subprocess.run(
            command,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if (
            completed.returncode == 2
            and "--ask-for-approval" in command
            and self._supports_ask_for_approval is not False
            and (
                "unknown option" in completed.stderr.lower()
                or "unexpected argument" in completed.stderr.lower()
            )
            and "ask-for-approval" in completed.stderr.lower()
        ):
            self._supports_ask_for_approval = False
            command = self._strip_ask_for_approval(command)
            completed = subprocess.run(
                command,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        elif (
            completed.returncode == 0
            and self._supports_ask_for_approval is None
            and "--ask-for-approval" in command
        ):
            self._supports_ask_for_approval = True

        return completed

    def _strip_ask_for_approval(self, command: list[str]) -> list[str]:
        cleaned: list[str] = []
        skip_next = False
        for index, token in enumerate(command):
            if skip_next:
                skip_next = False
                continue
            if token == "--ask-for-approval":
                if index + 1 < len(command):
                    skip_next = True
                continue
            cleaned.append(token)
        return cleaned

    def _build_command(self, request: ProviderRequest) -> list[str]:
        args: list[str] = [
            self.config.binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            self.config.sandbox,
            "--cd",
            request.cwd,
        ]
        if self._supports_ask_for_approval is not False:
            args.extend(["--ask-for-approval", self.config.ask_for_approval])
        if self.config.model:
            args.extend(["-m", self.config.model])
        args.append(self._format_prompt(request))
        return args

    def _format_prompt(self, request: ProviderRequest) -> str:
        lines = [
            "Developer instructions:",
            COCOA_DEVELOPER_INSTRUCTIONS,
            "",
            f"Workspace: {request.cwd}",
            f"Thread: {request.thread_id}",
            f"Turn: {request.turn_id}",
            "",
        ]
        if request.thread_context:
            lines.append("Recent thread context:")
            lines.append(request.thread_context)
            lines.append("")
        if request.workspace_context:
            lines.append("Workspace context:")
            lines.append(request.workspace_context)
            lines.append("")
        lines.append(request.prompt)
        return "\n".join(lines)

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
        item_type = item.get("type")
        details = item.get("details")
        if not isinstance(details, dict):
            details = {}

        if item_type is None and isinstance(details, dict):
            item_type = details.get("type")

        if item_type != "agent_message":
            return None

        text = item.get("text")
        if not isinstance(text, str):
            text = details.get("text")
        if text is None:
            return None
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
    provider = _resolve_provider_from_env(env)
    if provider in {"", "stub"}:
        return StubProvider()
    if provider in {"openai", "openai-compatible"}:
        config = OpenAICompatibleConfig.from_env(env, provider=provider)
        if config is None:
            raise ProviderConfigurationError("missing OpenAI configuration")
        return OpenAICompatibleProvider(config)
    if provider in {"codex-http", "codex-responses", "openai-codex"}:
        config = CodexResponsesConfig.from_env(env, provider=provider)
        if config is None:
            raise ProviderConfigurationError("missing OpenAI-compatible configuration")
        return CodexResponsesProvider(config)
    if provider == "codex":
        return CodexProvider(CodexConfig.from_env(env))
    raise ProviderConfigurationError(f"unsupported provider: {provider}")


def provider_name_from_env(env: Mapping[str, str] = os.environ) -> str:
    provider = _resolve_provider_from_env(env)
    if provider in {"", "stub"}:
        return "stub"
    if provider in {"openai", "openai-compatible"}:
        config = OpenAICompatibleConfig.from_env(env, provider=provider)
        if config is None:
            raise ProviderConfigurationError("missing OpenAI configuration")
        return f"openai-compatible:{config.model}"
    if provider in {"codex-http", "codex-responses", "openai-codex"}:
        config = CodexResponsesConfig.from_env(env, provider=provider)
        if config is None:
            raise ProviderConfigurationError("missing OpenAI-compatible configuration")
        return f"codex-http:{config.model}"
    if provider == "codex":
        config = CodexConfig.from_env(env, provider=provider)
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
