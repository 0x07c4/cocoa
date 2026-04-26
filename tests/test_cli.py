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
    _apply_completion_candidate,
    _apply_completion_candidate_at_cursor,
    _completion_candidates,
    _delete_previous_word_at_cursor,
    _effective_environment,
    _format_repl_prompt,
    _format_suggestion_lines,
    _pending_proposal_views,
    _print_pending_diff,
    _print_pending_proposals,
    _suggestion_items,
    _persist_environment,
    _parse_config_lines,
    _is_provider_configured,
    _resolve_provider_model,
    _resolve_provider_status,
    build_parser,
)
from cocoa.providers import ProviderRequest, ProviderResponse, StubProvider
from cocoa.runtime import AgentRuntime
from cocoa.store import JsonlStore


class CliFileProposalProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            message=(
                "I can update hello.txt.\n\n"
                "```cocoa-proposal\n"
                "{\"write_files\":[{\"path\":\"hello.txt\","
                "\"content\":\"new hello\\n\",\"reason\":\"refresh greeting\"}]}"
                "\n```"
            ),
            summary="file proposal",
        )


class CliAmbiguousEditProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            message=(
                "I can edit hello.txt.\n\n"
                "```cocoa-proposal\n"
                "{\"edits\":[{\"path\":\"hello.txt\","
                "\"old\":\"hello\",\"new\":\"hi\",\"reason\":\"ambiguous\"}]}"
                "\n```"
            ),
            summary="ambiguous edit",
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

    def test_repl_history_and_show_last_use_projection(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"COCOA_CODEX_HOME": tmpdir}, clear=True):
                with mock.patch(
                    "builtins.input",
                    side_effect=[
                        "hello projection",
                        "/history",
                        "/show last",
                        "/exit",
                    ],
                ):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        asyncio.run(run_repl(cwd=Path(tmpdir)))
        logs = output.getvalue()
        self.assertIn("conversation", logs)
        self.assertIn("2 items", logs)
        self.assertIn("turn:", logs)
        self.assertIn("user_message", logs)
        self.assertIn("agent_message", logs)
        self.assertIn("Provider is not configured yet.", logs)

    def test_repl_completion_includes_slash_commands(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            candidates = _completion_candidates(
                "/hi",
                "/hi",
                cwd=tmp_path,
                store=store,
                thread_id=thread.id,
            )

        self.assertIn("/history", candidates)

    def test_repl_completion_includes_show_targets(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)
            asyncio.run(runtime.run_user_turn(thread, "hello"))

            candidates = _completion_candidates(
                "/show ",
                "",
                cwd=tmp_path,
                store=store,
                thread_id=thread.id,
            )

        self.assertIn("last", candidates)
        self.assertTrue(any(candidate.startswith("turn_") for candidate in candidates))
        self.assertTrue(any(candidate.startswith("item_") for candidate in candidates))

    def test_repl_completion_includes_workspace_paths(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "src").mkdir()
            (tmp_path / "src" / "app.py").write_text("", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            candidates = _completion_candidates(
                "/inspect s",
                "s",
                cwd=tmp_path,
                store=store,
                thread_id=thread.id,
            )

        self.assertIn("src/", candidates)

    def test_repl_completion_includes_run_commands(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            bin_path = tmp_path / "bin"
            bin_path.mkdir()
            tool = bin_path / "cocoa-cat"
            tool.write_text("#!/bin/sh\n", encoding="utf-8")
            tool.chmod(0o755)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            with mock.patch.dict(os.environ, {"PATH": str(bin_path)}):
                candidates = _completion_candidates(
                    "/run cocoa-c",
                    "cocoa-c",
                    cwd=tmp_path,
                    store=store,
                    thread_id=thread.id,
                )

        self.assertIn("cocoa-cat", candidates)

    def test_repl_completion_includes_run_argument_paths(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "tmp").mkdir()
            (tmp_path / "tmp" / "cocoa-demo.txt").write_text("hello\n", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            candidates = _completion_candidates(
                "/run cat tmp/c",
                "tmp/c",
                cwd=tmp_path,
                store=store,
                thread_id=thread.id,
            )

        self.assertIn("tmp/cocoa-demo.txt", candidates)

    def test_repl_completion_includes_at_workspace_paths(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
            (tmp_path / "src").mkdir()
            (tmp_path / "src" / "app.py").write_text("", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            candidates = _completion_candidates(
                "explain @s",
                "@s",
                cwd=tmp_path,
                store=store,
                thread_id=thread.id,
            )
            suggestions = _suggestion_items(
                "explain @R",
                cwd=tmp_path,
                store=store,
                thread_id=thread.id,
            )

        self.assertIn("@src/", candidates)
        self.assertIn(("@README.md", "workspace context"), suggestions)

    def test_repl_completion_includes_pending_diff_and_reject_targets(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "hello.txt").write_text("old hello\n", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, CliFileProposalProvider())
            thread = runtime.start_thread(tmp_path)
            result = asyncio.run(runtime.run_user_turn_with_result(thread, "update"))
            item_id = result.proposals[0].id

            diff_candidates = _completion_candidates(
                f"/diff {item_id[:8]}",
                item_id[:8],
                cwd=tmp_path,
                store=store,
                thread_id=thread.id,
            )
            reject_candidates = _completion_candidates(
                f"/reject {item_id[:8]}",
                item_id[:8],
                cwd=tmp_path,
                store=store,
                thread_id=thread.id,
            )

        self.assertIn(item_id, diff_candidates)
        self.assertIn(item_id, reject_candidates)

    def test_pending_helpers_show_actions_and_diff(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "hello.txt").write_text("old hello\n", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, CliFileProposalProvider())
            thread = runtime.start_thread(tmp_path)
            result = asyncio.run(runtime.run_user_turn_with_result(thread, "update"))

            pending = _pending_proposal_views(store, thread.id)
            pending_output = io.StringIO()
            with redirect_stdout(pending_output):
                _print_pending_proposals(pending)
            diff_output = io.StringIO()
            with redirect_stdout(diff_output):
                _print_pending_diff(store, thread.id, result.proposals[0].id)

        self.assertIn("pending proposals:", pending_output.getvalue())
        self.assertIn(f"/diff {result.proposals[0].id}", pending_output.getvalue())
        self.assertIn(f"/apply {result.proposals[0].id}", pending_output.getvalue())
        self.assertIn(f"/reject {result.proposals[0].id}", pending_output.getvalue())
        self.assertIn("--- a/hello.txt", diff_output.getvalue())
        self.assertIn("+new hello", diff_output.getvalue())

    def test_pending_diff_explains_unusable_exact_edit(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "hello.txt").write_text("hello\nhello\n", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, CliAmbiguousEditProvider())
            thread = runtime.start_thread(tmp_path)
            result = asyncio.run(runtime.run_user_turn_with_result(thread, "update"))

            pending = _pending_proposal_views(store, thread.id)
            pending_output = io.StringIO()
            with redirect_stdout(pending_output):
                _print_pending_proposals(pending)
            diff_output = io.StringIO()
            with redirect_stdout(diff_output):
                _print_pending_diff(store, thread.id, result.proposals[0].id)

        self.assertIn("cannot apply:", pending_output.getvalue())
        self.assertIn("fresh @hello.txt context", pending_output.getvalue())
        self.assertNotIn(f"/apply {result.proposals[0].id}", pending_output.getvalue())
        self.assertIn("cannot show diff:", diff_output.getvalue())
        self.assertIn(f"reject: /reject {result.proposals[0].id}", diff_output.getvalue())

    def test_repl_suggestions_show_commands_without_tab(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            suggestions = _suggestion_items(
                "/",
                cwd=tmp_path,
                store=store,
                thread_id=thread.id,
            )

        self.assertIn(("/help", "show commands"), suggestions)
        self.assertIn(("/history", "show thread turns"), suggestions)
        self.assertIn(("/status", "show session status"), suggestions)

    def test_repl_suggestions_filter_commands_while_typing(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            suggestions = _suggestion_items(
                "/hi",
                cwd=tmp_path,
                store=store,
                thread_id=thread.id,
            )

        self.assertEqual(suggestions, [("/history", "show thread turns")])

    def test_apply_completion_adds_space_for_argument_commands(self) -> None:
        self.assertEqual(_apply_completion_candidate("/", "/show"), "/show ")
        self.assertEqual(_apply_completion_candidate("/show ", "last"), "/show last")

    def test_apply_completion_preserves_text_after_cursor(self) -> None:
        line, cursor = _apply_completion_candidate_at_cursor(
            "/sh later",
            len("/sh"),
            "/show",
        )

        self.assertEqual(line, "/show later")
        self.assertEqual(cursor, len("/show "))

    def test_apply_completion_replaces_current_token_suffix(self) -> None:
        line, cursor = _apply_completion_candidate_at_cursor(
            "/exit",
            len("/exi"),
            "/exit",
        )

        self.assertEqual(line, "/exit")
        self.assertEqual(cursor, len("/exit"))

    def test_apply_completion_replaces_at_reference_token(self) -> None:
        line, cursor = _apply_completion_candidate_at_cursor(
            "explain @RE later",
            len("explain @RE"),
            "@README.md",
        )

        self.assertEqual(line, "explain @README.md later")
        self.assertEqual(cursor, len("explain @README.md"))

    def test_delete_previous_word_at_cursor_preserves_suffix(self) -> None:
        line, cursor = _delete_previous_word_at_cursor("hello cocoa world", len("hello cocoa"))

        self.assertEqual(line, "hello world")
        self.assertEqual(cursor, len("hello "))

    def test_repl_suggestion_lines_mark_selected_item(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            lines = _format_suggestion_lines(
                "/",
                cwd=tmp_path,
                store=store,
                thread_id=thread.id,
                color=False,
                selected_index=1,
            )

        self.assertTrue(lines[0].startswith("  /accept"))
        self.assertTrue(lines[1].startswith("> /apply"))

    def test_repl_prompt_is_boxed_and_includes_status(self) -> None:
        prompt = _format_repl_prompt(
            "stub",
            "thr_test",
            color=False,
        )

        self.assertEqual(prompt, "+-- cocoa  stub  thr_test\n+> ")


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
