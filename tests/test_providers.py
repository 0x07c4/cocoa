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


if __name__ == "__main__":
    unittest.main()
