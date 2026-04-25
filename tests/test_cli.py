from __future__ import annotations

import asyncio
import io
from contextlib import redirect_stdout
from pathlib import Path
import os
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from cocoa.cli import (
    run_repl,
    _is_provider_configured,
    _resolve_provider_model,
    _resolve_provider_status,
    build_parser,
)


class CliTests(unittest.TestCase):
    def test_parser_defaults_use_repl_mode(self) -> None:
        args = build_parser().parse_args([])
        self.assertIsNone(args.command)
        self.assertEqual(args.cwd, ".")
        self.assertIsNone(args.thread)

    def test_provider_status_reports_stub_when_default(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"COCOA_CODEX_HOME": tmpdir}, clear=True):
                status = _resolve_provider_status()
        self.assertEqual(status, "stub")

    def test_provider_status_reports_missing_config(self) -> None:
        with mock.patch.dict(os.environ, {"COCOA_PROVIDER": "openai"}, clear=True):
            status = _resolve_provider_status()
        self.assertTrue(status.startswith("not configured ("))
        self.assertIn("missing", status)

    def test_provider_not_configured_by_default(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                os.environ,
                {"COCOA_CODEX_HOME": tmpdir},
                clear=True,
            ):
                self.assertFalse(_is_provider_configured())

    def test_provider_model_unknown_by_default(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                os.environ,
                {"COCOA_CODEX_HOME": tmpdir},
                clear=True,
            ):
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

    def test_repl_runs_with_invalid_provider_as_stub(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"COCOA_PROVIDER": "openai"}, clear=True):
                with mock.patch("builtins.input", side_effect=["/exit"]):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        asyncio.run(run_repl(cwd=Path(tmpdir)))
            logs = output.getvalue()
        self.assertIn("provider: not configured (missing", logs)
        self.assertIn("provider not ready, type /configure for setup", logs)

if __name__ == "__main__":
    unittest.main()
