from __future__ import annotations

import asyncio
import json
import os
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocoa.providers import (
    OpenAICompatibleConfig,
    CodexConfig,
    CodexProvider,
    OpenAICompatibleProvider,
    ProviderConfigurationError,
    ProviderHTTPError,
    ProviderRequest,
    ProviderResponseError,
    provider_from_env,
    provider_name_from_env,
)


class FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ProviderTests(unittest.TestCase):
    def test_openai_compatible_provider_calls_chat_completions(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout: float):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "provider ok",
                            }
                        }
                    ]
                }
            )

        config = OpenAICompatibleConfig(
            api_key="test-key",
            model="test-model",
            base_url="http://provider.example/v1",
            timeout_seconds=5,
        )
        provider = OpenAICompatibleProvider(config)

        with patch("urllib.request.urlopen", fake_urlopen):
            response = asyncio.run(
                provider.complete(
                    ProviderRequest(
                        thread_id="thr_test",
                        turn_id="turn_test",
                        prompt="hello",
                        cwd="/tmp/project",
                    )
                )
            )

        self.assertEqual(response.message, "provider ok")
        self.assertEqual(captured["url"], "http://provider.example/v1/chat/completions")
        self.assertEqual(captured["timeout"], 5)
        self.assertEqual(captured["payload"]["model"], "test-model")
        self.assertEqual(captured["payload"]["messages"][0]["role"], "developer")
        self.assertNotIn("item", captured["payload"])
        self.assertNotIn("event", captured["payload"])

    def test_openai_provider_includes_thread_context(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout: float):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "provider ok",
                            }
                        }
                    ]
                }
            )

        config = OpenAICompatibleConfig(
            api_key="test-key",
            model="test-model",
            base_url="http://provider.example/v1",
        )
        provider = OpenAICompatibleProvider(config)

        with patch("urllib.request.urlopen", fake_urlopen):
            asyncio.run(
                provider.complete(
                    ProviderRequest(
                        thread_id="thr_ctx",
                        turn_id="turn_ctx",
                        prompt="new question",
                        cwd="/tmp/project",
                        thread_context="User: first\nAssistant: done",
                    )
                )
            )

        payload = captured["payload"]  # type: ignore[assignment]
        self.assertIn("Recent thread context:", str(payload["messages"][1]["content"]))
        self.assertIn("User: first", str(payload["messages"][1]["content"]))

    def test_provider_rejects_missing_text_content(self) -> None:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(api_key="key", model="model")
        )

        with self.assertRaisesRegex(ProviderResponseError, "missing text content"):
            provider._extract_message({"choices": [{"message": {"content": None}}]})

    def test_provider_env_falls_back_to_stub_without_required_values(self) -> None:
        saved = {key: os.environ.get(key) for key in ["COCOA_PROVIDER"]}
        try:
            for key in saved:
                os.environ.pop(key, None)

            provider = provider_from_env()

            self.assertEqual(provider.__class__.__name__, "StubProvider")
            self.assertEqual(provider_name_from_env(), "stub")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_provider_env_requires_key_and_model_when_openai_selected(self) -> None:
        env = {"COCOA_PROVIDER": "openai"}

        with self.assertRaisesRegex(ProviderConfigurationError, "missing"):
            provider_from_env(env)

    def test_provider_env_builds_openai_provider(self) -> None:
        env = {
            "COCOA_PROVIDER": "openai",
            "COCOA_OPENAI_API_KEY": "secret",
            "COCOA_OPENAI_MODEL": "model",
            "COCOA_OPENAI_BASE_URL": "http://provider.example/v1",
        }

        provider = provider_from_env(env)

        self.assertEqual(provider.__class__.__name__, "OpenAICompatibleProvider")
        self.assertEqual(provider_name_from_env(env), "openai-compatible:model")

    def test_http_error_is_sanitized(self) -> None:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(api_key="secret-key", model="model")
        )

        class FakeHTTPError(Exception):
            code = 401

            def read(self) -> bytes:
                return json.dumps({"error": {"message": "bad key"}}).encode("utf-8")

        def fake_urlopen(request, timeout: float):
            import urllib.error

            raise urllib.error.HTTPError(
                url="http://provider.example/v1/chat/completions",
                code=401,
                msg="Unauthorized",
                hdrs={},
                fp=BytesIO(json.dumps({"error": {"message": "bad key"}}).encode("utf-8")),
            )

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(ProviderHTTPError) as raised:
                asyncio.run(
                    provider.complete(
                        ProviderRequest(
                            thread_id="thr",
                            turn_id="turn",
                            prompt="hello",
                            cwd="/tmp",
                        )
                    )
                )

        self.assertNotIn("secret-key", str(raised.exception))
        self.assertIn("provider HTTP 401", str(raised.exception))

    def test_provider_env_builds_codex_provider(self) -> None:
        env = {
            "COCOA_PROVIDER": "codex",
            "COCOA_CODEX_BINARY": "codex-test",
            "COCOA_CODEX_MODEL": "codex-model",
            "COCOA_CODEX_TIMEOUT_SECONDS": "12",
            "COCOA_CODEX_SANDBOX": "workspace-write",
            "COCOA_CODEX_APPROVAL": "never",
            "COCOA_CODEX_HOME": "/tmp/cocoa-codex",
        }
        provider = provider_from_env(env)

        self.assertEqual(provider.__class__.__name__, "CodexProvider")
        self.assertEqual(provider_name_from_env(env), "codex:codex-model")

    def test_codex_provider_builds_command_and_extracts_message(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(
            args: list[str],
            env: dict[str, str],
            check: bool,
            capture_output: bool,
            text: bool,
            timeout: float,
        ) -> FakeCompletedProcess:
            captured["args"] = args
            captured["env"] = env
            captured["timeout"] = timeout
            return FakeCompletedProcess(
                0,
                stdout="\n".join(
                    [
                        '{"type":"thread.started","thread_id":"019dc3"}',
                        '{"type":"turn.started"}',
                        (
                            '{"type":"item.completed","item":{"id":"item_0","details":'
                            '{"type":"agent_message","text":"from codex"}}}'
                        ),
                        '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":2,"reasoning_output_tokens":0}}',
                    ]
                )
                + "\n",
            )

        request = ProviderRequest(
            thread_id="thr_codex",
            turn_id="turn_codex",
            prompt="hello codex",
            cwd="/tmp/project",
        )
        provider = CodexProvider(
            CodexConfig(
                binary="codex-test",
                model="codex-model",
                timeout_seconds=4.0,
                sandbox="read-only",
                ask_for_approval="on-request",
                codex_home="/tmp/cocoa-codex-home",
            )
        )
        request = ProviderRequest(
            thread_id="thr_codex",
            turn_id="turn_codex",
            prompt="hello codex",
            cwd="/tmp/project",
            thread_context="User: first\nAssistant: done",
        )

        with patch("subprocess.run", fake_run):
            response = asyncio.run(provider.complete(request))

        self.assertEqual(response.message, "from codex")
        self.assertEqual(captured["args"][0], "codex-test")
        args = captured["args"]  # type: ignore[assignment]
        self.assertEqual(args[1], "exec")  # type: ignore[index]
        self.assertIn("--json", args)  # type: ignore[arg-type]
        self.assertIn("--skip-git-repo-check", args)  # type: ignore[arg-type]
        self.assertIn("-m", args)  # type: ignore[arg-type]
        self.assertIn("codex-model", args)  # type: ignore[arg-type]
        self.assertIn("--cd", args)  # type: ignore[arg-type]
        self.assertIn("/tmp/project", args)  # type: ignore[arg-type]
        self.assertIn("Workspace: /tmp/project", str(args[-1]))  # type: ignore[arg-type]
        self.assertEqual(captured["timeout"], 4.0)  # type: ignore[comparison-overlap]
        self.assertIn("Recent thread context:", str(args[-1]))  # type: ignore[arg-type]

        command_env = captured["env"]  # type: ignore[assignment]
        self.assertEqual(command_env.get("CODEX_HOME"), "/tmp/cocoa-codex-home")

    def test_codex_provider_error_when_exit_nonzero(self) -> None:
        def fake_run(
            args: list[str],
            env: dict[str, str],
            check: bool,
            capture_output: bool,
            text: bool,
            timeout: float,
        ) -> FakeCompletedProcess:
            return FakeCompletedProcess(
                7,
                stdout='{"type":"error","message":"codex stream failed"}',
                stderr="fatal: nope",
            )

        provider = CodexProvider(CodexConfig(binary="codex-test"))

        with patch("subprocess.run", fake_run):
            with self.assertRaisesRegex(
                ProviderResponseError, "codex exec failed with code 7: codex stream failed"
            ):
                asyncio.run(
                    provider.complete(
                        ProviderRequest(
                            thread_id="thr_codex_err",
                            turn_id="turn_codex_err",
                            prompt="oops",
                            cwd="/tmp/project",
                        )
                    )
                )

    def test_codex_provider_retries_without_ask_for_approval(self) -> None:
        calls: list[list[str]] = []

        def fake_run(
            args: list[str],
            env: dict[str, str],
            check: bool,
            capture_output: bool,
            text: bool,
            timeout: float,
        ) -> FakeCompletedProcess:
            calls.append(args)
            if len(calls) == 1:
                return FakeCompletedProcess(
                    2,
                    stdout="",
                    stderr="error: unknown option '--ask-for-approval'",
                )
            return FakeCompletedProcess(
                0,
                stdout="\n".join(
                    [
                        '{"type":"item.completed","item":{"id":"item_0","details":'
                        '{"type":"agent_message","text":"from codex"}}}'
                    ]
                )
                + "\n",
            )

        provider = CodexProvider(
            CodexConfig(
                binary="codex-test",
                model="codex-model",
                timeout_seconds=4.0,
                sandbox="read-only",
                ask_for_approval="on-request",
                codex_home="/tmp/cocoa-codex-home",
            )
        )

        with patch("subprocess.run", fake_run):
            response = asyncio.run(
                provider.complete(
                    ProviderRequest(
                        thread_id="thr_codex",
                        turn_id="turn_codex",
                        prompt="retry",
                        cwd="/tmp/project",
                    )
                )
            )

        self.assertEqual(response.message, "from codex")
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("--ask-for-approval", calls[0])
        self.assertNotIn("--ask-for-approval", calls[1])

if __name__ == "__main__":
    unittest.main()
