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
    _effective_environment,
    _persist_environment,
    _parse_config_lines,
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

    def test_parse_config_lines_handles_comments_quotes_and_spaces(self) -> None:
        parsed = _parse_config_lines(
            "\n".join(
                [
                    "# comment",
                    "COCOA_PROVIDER=openai",
                    "COCOA_OPENAI_API_KEY='sk test 123'",
                    "COCOA_OPENAI_MODEL=\"gpt 5.5\"",
                ]
            )
        )
        self.assertEqual(parsed["COCOA_PROVIDER"], "openai")
        self.assertEqual(parsed["COCOA_OPENAI_API_KEY"], "sk test 123")
        self.assertEqual(parsed["COCOA_OPENAI_MODEL"], "gpt 5.5")

    def test_effective_environment_reads_workspace_config(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / ".cocoa" / "config.env"
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(
                "\n".join(
                    [
                        "COCOA_PROVIDER=codex-http",
                        "COCOA_CODEX_API_KEY=workspace-token",
                        "COCOA_CODEX_MODEL=codex-workspace",
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {"COCOA_CODEX_HOME": tmpdir},
                clear=True,
            ):
                env = _effective_environment(Path(tmpdir))
            status = _resolve_provider_status(env)
            self.assertEqual(status, "codex-http:codex-workspace")

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

    def test_repl_configure_openai_persists_to_workspace_config(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"COCOA_CODEX_HOME": tmpdir}, clear=True):
                with mock.patch("builtins.input", side_effect=[
                    "/configure openai sk-test-key gpt-5",
                    "/provider",
                    "/exit",
                ]):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        asyncio.run(run_repl(cwd=Path(tmpdir)))
                cfg_path = Path(tmpdir) / ".cocoa" / "config.env"
                self.assertTrue(cfg_path.exists())
                cfg = cfg_path.read_text(encoding="utf-8")
                self.assertIn("COCOA_PROVIDER=openai", cfg)
                self.assertIn("COCOA_OPENAI_API_KEY=sk-test-key", cfg)
                self.assertIn("COCOA_OPENAI_MODEL=gpt-5", cfg)

        logs = output.getvalue()
        self.assertIn("provider config persisted to .cocoa/config.env", logs)
        self.assertIn("provider: openai-compatible:gpt-5", logs)

    def test_repl_set_updates_provider_in_session(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"COCOA_CODEX_HOME": tmpdir}, clear=True):
                with mock.patch("builtins.input", side_effect=[
                    "/set COCOA_PROVIDER=openai",
                    "/set COCOA_OPENAI_API_KEY=sk-session-key",
                    "/set COCOA_OPENAI_MODEL gpt-5",
                    "/provider",
                    "/exit",
                ]):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        asyncio.run(run_repl(cwd=Path(tmpdir)))
        logs = output.getvalue()
        self.assertIn("COCOA_PROVIDER set", logs)
        self.assertIn("provider: openai-compatible:gpt-5", logs)

    def test_repl_set_persist_in_session(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"COCOA_CODEX_HOME": tmpdir}, clear=True):
                with mock.patch(
                    "builtins.input",
                    side_effect=[
                        "/set --persist COCOA_PROVIDER=openai",
                        "/set --persist COCOA_OPENAI_API_KEY=sk-session-persist",
                        "/set --persist COCOA_OPENAI_MODEL=gpt-5",
                        "/exit",
                    ],
                ):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        asyncio.run(run_repl(cwd=Path(tmpdir)))
            cfg_path = Path(tmpdir) / ".cocoa" / "config.env"
            self.assertTrue(cfg_path.exists())
            cfg = cfg_path.read_text(encoding="utf-8")
            self.assertIn("COCOA_PROVIDER=openai", cfg)
            self.assertIn("COCOA_OPENAI_API_KEY=sk-session-persist", cfg)
            self.assertIn("COCOA_OPENAI_MODEL=gpt-5", cfg)

        logs = output.getvalue()
        self.assertIn("COCOA_PROVIDER persisted and set", logs)
        self.assertIn("COCOA_OPENAI_API_KEY persisted and set", logs)
        self.assertIn("COCOA_OPENAI_MODEL persisted and set", logs)

    def test_repl_persist_command_writes_overrides(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"COCOA_CODEX_HOME": tmpdir}, clear=True):
                with mock.patch(
                    "builtins.input",
                    side_effect=[
                        "/set COCOA_PROVIDER=openai",
                        "/set COCOA_OPENAI_API_KEY=sk-session-persist2",
                        "/set COCOA_OPENAI_MODEL=gpt-4.1",
                        "/persist",
                        "/provider",
                        "/exit",
                    ],
                ):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        asyncio.run(run_repl(cwd=Path(tmpdir)))
            cfg_path = Path(tmpdir) / ".cocoa" / "config.env"
            self.assertTrue(cfg_path.exists())
            cfg = cfg_path.read_text(encoding="utf-8")
            self.assertIn("COCOA_PROVIDER=openai", cfg)
            self.assertIn("COCOA_OPENAI_API_KEY=sk-session-persist2", cfg)
            self.assertIn("COCOA_OPENAI_MODEL=gpt-4.1", cfg)
        logs = output.getvalue()
        self.assertIn("session overrides persisted to .cocoa/config.env", logs)
        self.assertIn("provider: openai-compatible:gpt-4.1", logs)


    def test_persist_environment_overwrites_keys(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / ".cocoa" / "config.env"
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text("COCOA_PROVIDER=codex-http\nCOCOA_CODEX_MODEL=old\n", encoding="utf-8")
            _persist_environment(Path(tmpdir), {"COCOA_PROVIDER": "openai", "COCOA_OPENAI_MODEL": "gpt-5-mini"})
            cfg = cfg_path.read_text(encoding="utf-8")
        self.assertIn("COCOA_PROVIDER=openai", cfg)
        self.assertIn("COCOA_OPENAI_MODEL=gpt-5-mini", cfg)
        self.assertIn("COCOA_CODEX_MODEL=old", cfg)

if __name__ == "__main__":
    unittest.main()
