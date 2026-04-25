from __future__ import annotations

import os
import unittest
from unittest import mock

from cocoa.cli import _resolve_provider_model, _resolve_provider_status, build_parser


class CliTests(unittest.TestCase):
    def test_parser_defaults_use_repl_mode(self) -> None:
        args = build_parser().parse_args([])
        self.assertIsNone(args.command)
        self.assertEqual(args.cwd, ".")
        self.assertIsNone(args.thread)

    def test_provider_status_reports_stub_when_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            status = _resolve_provider_status()
        self.assertEqual(status, "stub")

    def test_provider_status_reports_missing_config(self) -> None:
        with mock.patch.dict(os.environ, {"COCOA_PROVIDER": "openai"}, clear=True):
            status = _resolve_provider_status()
        self.assertTrue(status.startswith("not configured ("))
        self.assertIn("missing", status)

    def test_provider_model_unknown_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            model = _resolve_provider_model()
        self.assertEqual(model, "unknown")

    def test_provider_model_for_openai_config(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "COCOA_PROVIDER": "openai",
                "COCOA_OPENAI_API_KEY": "test-key",
                "COCOA_OPENAI_MODEL": "gpt-5-mini",
            },
            clear=True,
        ):
            model = _resolve_provider_model()
        self.assertEqual(model, "gpt-5-mini")

    def test_provider_model_for_codex_http_config(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "COCOA_PROVIDER": "codex-http",
                "COCOA_CODEX_API_KEY": "test-key",
                "COCOA_CODEX_MODEL": "codex-mini",
            },
            clear=True,
        ):
            model = _resolve_provider_model()
        self.assertEqual(model, "codex-mini")


if __name__ == "__main__":
    unittest.main()
