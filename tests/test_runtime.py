from pathlib import Path
import asyncio
import json
import shlex
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocoa.providers import ProviderRequest, ProviderResponse, StubProvider
from cocoa.runtime import AgentRuntime
from cocoa.store import JsonlStore
from cocoa.tools import AlwaysApprovePrompter, AlwaysRejectPrompter, ShellTool


class FailingProvider:
    async def complete(self, request):
        raise RuntimeError("provider exploded")


class CapturingProvider:
    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return ProviderResponse(
            message=f"echo:{request.prompt}",
            summary="captured",
        )


class ProposalProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            message=(
                "I can run a verification command.\n\n"
                "```cocoa-proposal\n"
                "{\"commands\":[{\"command\":\""
                f"{shlex.quote(sys.executable)} -c \\\"print('proposal')\\\""
                "\",\"reason\":\"verify proposal execution\"}]}"
                "\n```"
            ),
            summary="proposal",
        )


class FileProposalProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        proposal = {
            "write_files": [
                {
                    "path": "hello.txt",
                    "content": "hello from cocoa\n",
                    "reason": "create a demo file",
                }
            ]
        }
        return ProviderResponse(
            message=(
                "I can create a file.\n\n"
                "```cocoa-proposal\n"
                f"{json.dumps(proposal)}\n"
                "```"
            ),
            summary="file proposal",
        )


class FileEditProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        proposal = {
            "edits": [
                {
                    "path": "hello.txt",
                    "old": "hello from cocoa\n",
                    "new": "hello from edited cocoa\n",
                    "reason": "update greeting",
                }
            ]
        }
        return ProviderResponse(
            message=(
                "I can edit a file.\n\n"
                "```cocoa-proposal\n"
                f"{json.dumps(proposal)}\n"
                "```"
            ),
            summary="file edit proposal",
        )


class RuntimeTests(unittest.TestCase):
    def test_runtime_records_user_turn(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            message = asyncio.run(runtime.run_user_turn(thread, "hello"))
            rows = store.read_thread(thread.id)

            self.assertIn("Provider is not configured yet.", message)
            self.assertEqual(
                [row["kind"] for row in rows],
                [
                    "thread_started",
                    "turn_started",
                    "item_completed",
                    "routing_decision",
                    "usage_recorded",
                    "item_completed",
                    "turn_completed",
                ],
            )

    def test_runtime_records_routing_and_usage_events(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, CapturingProvider())
            thread = runtime.start_thread(tmp_path)

            result = asyncio.run(
                runtime.run_user_turn_with_result(
                    thread,
                    "hello usage",
                    routing={
                        "mode": "cheap",
                        "provider": "openai-compatible",
                        "model": "deepseek-v4",
                        "reason": "test route",
                    },
                )
            )
            rows = store.read_thread(thread.id)

            routing = next(row for row in rows if row["kind"] == "routing_decision")
            usage = next(row for row in rows if row["kind"] == "usage_recorded")
            self.assertEqual(routing["payload"]["mode"], "cheap")
            self.assertEqual(routing["payload"]["model"], "deepseek-v4")
            self.assertEqual(usage["payload"]["provider"], "openai-compatible")
            self.assertGreater(usage["payload"]["input_tokens"], 0)
            self.assertGreater(usage["payload"]["output_tokens"], 0)
            self.assertTrue(result.usage)

    def test_runtime_runs_approved_shell_turn(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)
            shell = ShellTool(AlwaysApprovePrompter())
            command = f"{shlex.quote(sys.executable)} -c \"print('cocoa')\""

            result = asyncio.run(runtime.run_shell_turn(thread, command, shell))
            rows = store.read_thread(thread.id)

            self.assertTrue(result.approved)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout.strip(), "cocoa")
            self.assertIn("approval_requested", [row["kind"] for row in rows])
            self.assertIn("approval_resolved", [row["kind"] for row in rows])

    def test_runtime_rejects_shell_turn_before_execution(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            marker = tmp_path / "marker"
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)
            shell = ShellTool(AlwaysRejectPrompter())
            command = f"{shlex.quote(sys.executable)} -c \"open({str(marker)!r}, 'w').write('bad')\""

            result = asyncio.run(runtime.run_shell_turn(thread, command, shell))

            self.assertFalse(result.approved)
            self.assertIsNone(result.exit_code)
            self.assertFalse(marker.exists())

    def test_runtime_closes_turn_on_provider_error(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, FailingProvider())
            thread = runtime.start_thread(tmp_path)

            with self.assertRaisesRegex(RuntimeError, "provider exploded"):
                asyncio.run(runtime.run_user_turn(thread, "hello"))

            rows = store.read_thread(thread.id)
            self.assertIn("error", [row["kind"] for row in rows])
            self.assertEqual(rows[-1]["kind"], "turn_completed")
            self.assertEqual(rows[-1]["payload"]["turn"]["status"], "failed")

    def test_runtime_marks_nonzero_shell_exit_failed(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)
            shell = ShellTool(AlwaysApprovePrompter())
            command = f"{shlex.quote(sys.executable)} -c \"raise SystemExit(7)\""

            result = asyncio.run(runtime.run_shell_turn(thread, command, shell))
            rows = store.read_thread(thread.id)

            self.assertTrue(result.approved)
            self.assertEqual(result.exit_code, 7)
            self.assertEqual(rows[-2]["payload"]["item"]["status"], "failed")
            self.assertEqual(rows[-1]["payload"]["turn"]["status"], "failed")

    def test_runtime_marks_shell_timeout_failed(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)
            shell = ShellTool(AlwaysApprovePrompter(), timeout_seconds=1)
            command = f"{shlex.quote(sys.executable)} -c \"import time; time.sleep(10)\""

            result = asyncio.run(runtime.run_shell_turn(thread, command, shell))
            rows = store.read_thread(thread.id)

            self.assertTrue(result.timed_out)
            self.assertEqual(rows[-2]["payload"]["item"]["status"], "failed")
            self.assertEqual(rows[-1]["payload"]["turn"]["status"], "failed")

    def test_runtime_reuses_thread_context(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)
            asyncio.run(runtime.run_user_turn(thread, "first"))
            asyncio.run(runtime.run_user_turn(thread, "second"))

            request = provider.requests[-1]
            self.assertIsNotNone(request.thread_context)
            self.assertIn("User: first", request.thread_context)
            self.assertIn("Assistant: echo:first", request.thread_context)

    def test_runtime_reuses_command_results_in_thread_context(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)
            shell = ShellTool(AlwaysApprovePrompter())
            command = (
                f"{shlex.quote(sys.executable)} -c "
                "\"import sys; print('tool stdout'); print('tool stderr', file=sys.stderr); raise SystemExit(7)\""
            )
            asyncio.run(runtime.run_shell_turn(thread, command, shell))

            asyncio.run(runtime.run_user_turn(thread, "what failed?"))

            request = provider.requests[-1]
            self.assertIsNotNone(request.thread_context)
            context = request.thread_context or ""
            self.assertIn("User: /run", context)
            self.assertIn("Command (failed", context)
            self.assertIn("exit_code: 7", context)
            self.assertIn("tool stdout", context)
            self.assertIn("tool stderr", context)

    def test_runtime_reuses_file_write_results_in_thread_context(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            proposal_runtime = AgentRuntime(store, FileProposalProvider())
            thread = proposal_runtime.start_thread(tmp_path)
            turn_result = asyncio.run(
                proposal_runtime.run_user_turn_with_result(thread, "write file")
            )
            proposal_runtime.apply_proposed_file_write(thread, turn_result.proposals[0].id)

            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            asyncio.run(runtime.run_user_turn(thread, "what changed?"))

            request = provider.requests[-1]
            self.assertIsNotNone(request.thread_context)
            context = request.thread_context or ""
            self.assertIn("File write (completed, approval=accepted): hello.txt", context)
            self.assertIn("bytes_written:", context)

    def test_runtime_reuses_unusable_pending_edit_in_thread_context(self) -> None:
        from tempfile import TemporaryDirectory

        class AmbiguousEditProvider:
            async def complete(self, request: ProviderRequest) -> ProviderResponse:
                proposal = {
                    "edits": [
                        {
                            "path": "hello.txt",
                            "old": "hello",
                            "new": "hi",
                            "reason": "ambiguous",
                        }
                    ]
                }
                return ProviderResponse(
                    message="bad edit\n```cocoa-proposal\n"
                    f"{json.dumps(proposal)}\n```",
                    summary="bad edit",
                )

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "hello.txt").write_text("hello\nhello\n", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            proposal_runtime = AgentRuntime(store, AmbiguousEditProvider())
            thread = proposal_runtime.start_thread(tmp_path)
            asyncio.run(proposal_runtime.run_user_turn_with_result(thread, "edit file"))

            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            asyncio.run(runtime.run_user_turn(thread, "try again"))

            request = provider.requests[-1]
            self.assertIsNotNone(request.thread_context)
            context = request.thread_context or ""
            self.assertIn("File edit (pending", context)
            self.assertIn("cannot_apply: old text matched 2 times", context)

    def test_runtime_includes_workspace_map_context(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "what is here?"))

            request = provider.requests[-1]
            self.assertIsNotNone(request.workspace_context)
            self.assertIn("Workspace file map", request.workspace_context or "")
            self.assertIn("app.py", request.workspace_context or "")

    def test_runtime_includes_date_in_workspace_context(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "hello"))

            request = provider.requests[-1]
            self.assertIsNotNone(request.workspace_context)
            self.assertIn("Current date and time:", request.workspace_context or "")

    def test_runtime_includes_git_status_in_workspace_context(self) -> None:
        from tempfile import TemporaryDirectory
        import subprocess

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@test"], cwd=tmp_path, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
            (tmp_path / "readme.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "status?"))

            request = provider.requests[-1]
            self.assertIsNotNone(request.workspace_context)
            self.assertIn("Git status:", request.workspace_context or "")

    def test_runtime_includes_instruction_files_in_workspace_context(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "AGENTS.md").write_text(
                "# Agent Instructions\n\nBe helpful.\n", encoding="utf-8"
            )
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "hello"))

            request = provider.requests[-1]
            self.assertIsNotNone(request.workspace_context)
            self.assertIn("Instruction file:", request.workspace_context or "")
            self.assertIn("AGENTS.md", request.workspace_context or "")
            self.assertIn("Be helpful.", request.workspace_context or "")

    def test_runtime_reads_prompt_path_references_as_context_items(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            source = tmp_path / "src" / "app.py"
            source.parent.mkdir()
            source.write_text("def main():\n    return 'cocoa'\n", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)

            result = asyncio.run(
                runtime.run_user_turn_with_result(thread, "explain @src/app.py")
            )
            rows = store.read_thread(thread.id)

            self.assertEqual(len(result.context_items), 1)
            item = result.context_items[0]
            self.assertEqual(item.kind, "file_read")
            self.assertEqual(item.status, "completed")
            self.assertEqual(item.content["path"], "src/app.py")
            self.assertIn("def main", item.content["text"])
            request = provider.requests[-1]
            self.assertIn("<file path=\"src/app.py\">", request.workspace_context or "")
            self.assertIn("return 'cocoa'", request.workspace_context or "")
            item_kinds = [
                row["payload"].get("item", {}).get("kind")
                for row in rows
                if isinstance(row.get("payload"), dict)
            ]
            self.assertIn("file_read", item_kinds)

    def test_runtime_records_failed_prompt_path_reference(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)

            result = asyncio.run(
                runtime.run_user_turn_with_result(thread, "explain @missing.py")
            )

            self.assertEqual(len(result.context_items), 1)
            item = result.context_items[0]
            self.assertEqual(item.kind, "file_read")
            self.assertEqual(item.status, "failed")
            self.assertEqual(item.content["path"], "missing.py")
            self.assertIn("unavailable", provider.requests[-1].workspace_context or "")

    def test_runtime_can_resume_thread(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path, title="resume test")
            asyncio.run(runtime.run_user_turn(thread, "hello"))

            resumed = runtime.resume_thread(thread.id)

            self.assertEqual(resumed.id, thread.id)
            self.assertEqual(resumed.cwd, thread.cwd)
            self.assertEqual(resumed.title, "resume test")

            message = asyncio.run(runtime.run_user_turn(resumed, "again"))
            self.assertIn("Provider is not configured yet.", message)

    def test_runtime_records_provider_command_proposals(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, ProposalProvider())
            thread = runtime.start_thread(tmp_path)

            result = asyncio.run(runtime.run_user_turn_with_result(thread, "verify"))
            rows = store.read_thread(thread.id)

            self.assertEqual(result.message, "I can run a verification command.")
            self.assertEqual(len(result.proposals), 1)
            proposal = result.proposals[0]
            self.assertEqual(proposal.kind, "command")
            self.assertEqual(proposal.status, "pending")
            self.assertEqual(proposal.approval, "requested")
            self.assertEqual(proposal.content["reason"], "verify proposal execution")
            self.assertIn("approval_requested", [row["kind"] for row in rows])

    def test_runtime_accepts_provider_command_proposal(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, ProposalProvider())
            thread = runtime.start_thread(tmp_path)
            shell = ShellTool(AlwaysApprovePrompter())

            turn_result = asyncio.run(runtime.run_user_turn_with_result(thread, "verify"))
            command_result = asyncio.run(
                runtime.run_proposed_command(thread, turn_result.proposals[0].id, shell)
            )
            rows = store.read_thread(thread.id)

            self.assertTrue(command_result.approved)
            self.assertEqual(command_result.exit_code, 0)
            self.assertEqual(command_result.stdout.strip(), "proposal")
            self.assertEqual(rows[-1]["payload"]["item"]["status"], "completed")

    def test_runtime_records_provider_file_write_proposals(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, FileProposalProvider())
            thread = runtime.start_thread(tmp_path)

            result = asyncio.run(runtime.run_user_turn_with_result(thread, "write file"))

            self.assertEqual(result.message, "I can create a file.")
            self.assertEqual(len(result.proposals), 1)
            proposal = result.proposals[0]
            self.assertEqual(proposal.kind, "file_write")
            self.assertEqual(proposal.status, "pending")
            self.assertEqual(proposal.approval, "requested")
            self.assertEqual(proposal.content["path"], "hello.txt")
            self.assertEqual(proposal.content["reason"], "create a demo file")
            self.assertIn("+hello from cocoa", proposal.content["diff"])

    def test_runtime_applies_provider_file_write_proposal(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, FileProposalProvider())
            thread = runtime.start_thread(tmp_path)

            turn_result = asyncio.run(runtime.run_user_turn_with_result(thread, "write file"))
            item = runtime.apply_proposed_file_write(thread, turn_result.proposals[0].id)
            rows = store.read_thread(thread.id)

            self.assertEqual((tmp_path / "hello.txt").read_text(encoding="utf-8"), "hello from cocoa\n")
            self.assertEqual(item.status, "completed")
            self.assertEqual(item.approval, "accepted")
            self.assertEqual(rows[-1]["payload"]["item"]["status"], "completed")

    def test_runtime_records_provider_file_edit_proposals(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "hello.txt").write_text("hello from cocoa\n", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, FileEditProvider())
            thread = runtime.start_thread(tmp_path)

            result = asyncio.run(runtime.run_user_turn_with_result(thread, "edit file"))

            self.assertEqual(result.message, "I can edit a file.")
            self.assertEqual(len(result.proposals), 1)
            proposal = result.proposals[0]
            self.assertEqual(proposal.kind, "file_write")
            self.assertEqual(proposal.status, "pending")
            self.assertEqual(proposal.approval, "requested")
            self.assertEqual(proposal.content["operation"], "replace")
            self.assertEqual(proposal.content["path"], "hello.txt")
            self.assertEqual(proposal.content["reason"], "update greeting")
            self.assertIn("-hello from cocoa", proposal.content["diff"])
            self.assertIn("+hello from edited cocoa", proposal.content["diff"])

    def test_runtime_applies_provider_file_edit_proposal(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "hello.txt").write_text("hello from cocoa\n", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, FileEditProvider())
            thread = runtime.start_thread(tmp_path)

            turn_result = asyncio.run(runtime.run_user_turn_with_result(thread, "edit file"))
            item = runtime.apply_proposed_file_write(thread, turn_result.proposals[0].id)

            self.assertEqual(
                (tmp_path / "hello.txt").read_text(encoding="utf-8"),
                "hello from edited cocoa\n",
            )
            self.assertEqual(item.status, "completed")
            self.assertEqual(item.approval, "accepted")
            self.assertEqual(item.content["operation"], "replace")

    def test_runtime_rejects_ambiguous_file_edit_proposal(self) -> None:
        from tempfile import TemporaryDirectory

        class AmbiguousEditProvider:
            async def complete(self, request: ProviderRequest) -> ProviderResponse:
                proposal = {
                    "edits": [
                        {
                            "path": "hello.txt",
                            "old": "hello",
                            "new": "hi",
                            "reason": "ambiguous",
                        }
                    ]
                }
                return ProviderResponse(
                    message="bad edit\n```cocoa-proposal\n"
                    f"{json.dumps(proposal)}\n```",
                    summary="bad edit",
                )

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "hello.txt").write_text("hello\nhello\n", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, AmbiguousEditProvider())
            thread = runtime.start_thread(tmp_path)

            turn_result = asyncio.run(runtime.run_user_turn_with_result(thread, "edit file"))

            self.assertIn("matched 2 times", turn_result.proposals[0].content["scope_error"])
            with self.assertRaisesRegex(ValueError, "matched 2 times"):
                runtime.apply_proposed_file_write(thread, turn_result.proposals[0].id)

    def test_runtime_rejects_pending_file_write_proposal(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, FileProposalProvider())
            thread = runtime.start_thread(tmp_path)

            turn_result = asyncio.run(runtime.run_user_turn_with_result(thread, "write file"))
            item = runtime.reject_pending_item(thread, turn_result.proposals[0].id)
            rows = store.read_thread(thread.id)

            self.assertFalse((tmp_path / "hello.txt").exists())
            self.assertEqual(item.status, "rejected")
            self.assertEqual(item.approval, "rejected")
            self.assertEqual(rows[-2]["kind"], "approval_resolved")
            self.assertEqual(rows[-2]["payload"]["approved"], False)
            self.assertEqual(rows[-1]["payload"]["item"]["status"], "rejected")

    def test_runtime_rejects_file_write_path_escape_on_apply(self) -> None:
        from tempfile import TemporaryDirectory

        class EscapingFileProvider:
            async def complete(self, request: ProviderRequest) -> ProviderResponse:
                proposal = {
                    "write_files": [
                        {"path": "../outside.txt", "content": "bad", "reason": "escape"}
                    ]
                }
                return ProviderResponse(
                    message="bad proposal\n```cocoa-proposal\n"
                    f"{json.dumps(proposal)}\n```",
                    summary="bad",
                )

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, EscapingFileProvider())
            thread = runtime.start_thread(tmp_path)

            turn_result = asyncio.run(runtime.run_user_turn_with_result(thread, "write file"))

            with self.assertRaisesRegex(ValueError, "escapes workspace"):
                runtime.apply_proposed_file_write(thread, turn_result.proposals[0].id)

    def test_escalation_raises_error_with_no_prior_turn(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            with self.assertRaisesRegex(ValueError, "no previous turn to escalate"):
                asyncio.run(runtime.run_escalation_turn(thread, target="last"))

    def test_escalation_calls_provider_with_target_context(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "hello world"))

            result = asyncio.run(runtime.run_escalation_turn(thread, target="last"))

            self.assertIn("hello world", result.message)
            self.assertEqual(len(provider.requests), 2)
            request = provider.requests[-1]
            self.assertIsNone(request.workspace_context)
            self.assertIn("Target Turn ID", request.prompt)
            self.assertIn("hello world", request.prompt)
            self.assertIn("ready", request.prompt.lower())

    def test_escalation_records_routing_and_usage_metadata(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, CapturingProvider())
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "test route"))
            result = asyncio.run(
                runtime.run_escalation_turn(
                    thread,
                    target="last",
                    routing={
                        "mode": "premium",
                        "provider": "codex-http",
                        "model": "codex-review",
                        "reason": "test escalation",
                    },
                )
            )

            rows = store.read_thread(thread.id)
            routing = next(row for row in rows if row["kind"] == "routing_decision"
                           and row["turn_id"] != rows[1]["turn_id"])
            usage = next(row for row in rows if row["kind"] == "usage_recorded"
                         and row["turn_id"] == routing["turn_id"])

            self.assertEqual(routing["payload"]["role"], "review")
            self.assertTrue(routing["payload"]["escalation"])
            self.assertIn("target_turn_id", routing["payload"])
            self.assertIn(result.usage, [usage["payload"]])

    def test_escalation_skips_prior_escalation_turns(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "first turn"))
            asyncio.run(runtime.run_escalation_turn(thread, target="last"))
            provider.requests.clear()

            result = asyncio.run(runtime.run_user_turn(thread, "second turn"))
            provider.requests.clear()

            result = asyncio.run(runtime.run_escalation_turn(thread, target="last"))

            self.assertIn("second turn", result.message)

    def test_escalation_does_not_parse_proposals(self) -> None:
        from tempfile import TemporaryDirectory
        import json

        class ProposalInResponseProvider:
            async def complete(self, request):
                return ProviderResponse(
                    message="review ok\n```cocoa-proposal\n"
                    "{\"commands\":[{\"command\":\"echo bad\",\"reason\":\"should not parse\"}]}"
                    "\n```",
                    summary="review with proposal",
                )

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, CapturingProvider())
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "write proposal"))
            store2 = JsonlStore.for_workspace(tmp_path)
            runtime2 = AgentRuntime(store2, ProposalInResponseProvider())

            result = asyncio.run(
                runtime2.run_escalation_turn(thread, target="last")
            )

            self.assertIn("review ok", result.message)
            self.assertFalse(result.proposals)

    def test_escalation_fails_for_unsupported_target(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            with self.assertRaisesRegex(ValueError, "unsupported escalation target"):
                asyncio.run(runtime.run_escalation_turn(thread, target="all"))

    def test_escalation_includes_command_results_in_prompt(self) -> None:
        from tempfile import TemporaryDirectory
        import shlex
        import sys

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)
            shell = ShellTool(AlwaysApprovePrompter())
            command = f"{shlex.quote(sys.executable)} -c \"print('cmd_out')\""

            asyncio.run(runtime.run_shell_turn(thread, command, shell))

            provider = CapturingProvider()
            store2 = JsonlStore.for_workspace(tmp_path)
            runtime2 = AgentRuntime(store2, provider)

            result = asyncio.run(
                runtime2.run_escalation_turn(thread, target="last")
            )

            self.assertIn("cmd_out", result.message)
            request = provider.requests[-1]
            self.assertIn("Command ", request.prompt)
            self.assertIn("cmd_out", request.prompt)
            self.assertIn("exit_code: 0", request.prompt)

    def test_escalation_includes_pending_proposals_in_prompt(self) -> None:
        from tempfile import TemporaryDirectory
        import json

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, FileProposalProvider())
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn_with_result(thread, "write file"))
            provider = CapturingProvider()
            store2 = JsonlStore.for_workspace(tmp_path)
            runtime2 = AgentRuntime(store2, provider)

            result = asyncio.run(
                runtime2.run_escalation_turn(thread, target="last")
            )

            request = provider.requests[-1]
            self.assertIn("File write proposal:", request.prompt)
            self.assertIn("hello.txt", request.prompt)
            self.assertIn("create a demo file", request.prompt)

    def test_escalation_skips_turn_with_no_items(self) -> None:
        from tempfile import TemporaryDirectory

        class EmptyTurnCapturingProvider:
            def __init__(self):
                self.requests = []

            async def complete(self, request):
                self.requests.append(request)
                return ProviderResponse(
                    message=f"echo:{request.prompt}",
                    summary="captured",
                )

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            provider = EmptyTurnCapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "real turn"))
            provider.requests.clear()

            result = asyncio.run(
                runtime.run_escalation_turn(thread, target="last")
            )

            self.assertIn("real turn", result.message)

    def test_escalation_reuses_thread_context(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "first turn"))
            asyncio.run(runtime.run_user_turn(thread, "second turn"))
            provider.requests.clear()

            asyncio.run(runtime.run_escalation_turn(thread, target="last"))

            request = provider.requests[-1]
            self.assertIsNotNone(request.thread_context)
            self.assertIn("User: first turn", request.thread_context)

    def test_escalation_does_not_include_workspace_context(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "what is here?"))
            provider.requests.clear()

            asyncio.run(runtime.run_escalation_turn(thread, target="last"))

            request = provider.requests[-1]
            self.assertIsNone(request.workspace_context)

    def test_escalation_can_escalate_same_turn_multiple_times(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            provider = CapturingProvider()
            runtime = AgentRuntime(store, provider)
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "only turn"))
            asyncio.run(runtime.run_escalation_turn(thread, target="last"))
            provider.requests.clear()

            result = asyncio.run(
                runtime.run_escalation_turn(thread, target="last")
            )

            self.assertIn("only turn", result.message)

    def test_runtime_rejects_ignored_file_write_path_on_apply(self) -> None:
        from tempfile import TemporaryDirectory

        class IgnoredFileProvider:
            async def complete(self, request: ProviderRequest) -> ProviderResponse:
                proposal = {
                    "write_files": [
                        {"path": ".cocoa/config.env", "content": "bad", "reason": "ignored"}
                    ]
                }
                return ProviderResponse(
                    message="bad proposal\n```cocoa-proposal\n"
                    f"{json.dumps(proposal)}\n```",
                    summary="bad",
                )

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, IgnoredFileProvider())
            thread = runtime.start_thread(tmp_path)

            turn_result = asyncio.run(runtime.run_user_turn_with_result(thread, "write file"))

            with self.assertRaisesRegex(ValueError, "path is ignored"):
                runtime.apply_proposed_file_write(thread, turn_result.proposals[0].id)


    def test_runtime_creates_task_item(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            item = runtime.create_task(
                thread,
                "Implement login",
                description="Add user authentication",
                owner="deepseek",
                blocks=["auth_design"],
                blocked_by=["db_setup"],
                metadata={"priority": "high"},
            )
            rows = store.read_thread(thread.id)

            self.assertEqual(item.kind, "task")
            self.assertEqual(item.content["subject"], "Implement login")
            self.assertEqual(item.content["description"], "Add user authentication")
            self.assertEqual(item.content["status"], "pending")
            self.assertEqual(item.content["owner"], "deepseek")
            self.assertEqual(item.content["blocks"], ["auth_design"])
            self.assertEqual(item.content["blocked_by"], ["db_setup"])
            self.assertEqual(item.content["metadata"], {"priority": "high"})
            event_kinds = [row["kind"] for row in rows]
            self.assertIn("turn_started", event_kinds)
            self.assertIn("item_completed", event_kinds)
            self.assertIn("turn_completed", event_kinds)

    def test_runtime_updates_task_status(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            item = runtime.create_task(thread, "Implement login")
            updated = runtime.update_task(thread, item.id, status="in_progress")
            rows = store.read_thread(thread.id)

            self.assertEqual(updated.content["status"], "in_progress")
            updated_events = [
                row for row in rows if row["kind"] == "item_updated"
            ]
            self.assertEqual(len(updated_events), 1)

    def test_runtime_updates_task_metadata_merge(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            item = runtime.create_task(
                thread, "Refactor", metadata={"priority": "low", "tags": ["cleanup"]}
            )
            updated = runtime.update_task(
                thread, item.id, metadata={"priority": "high", "assignee": "alice"}
            )

            self.assertEqual(updated.content["metadata"]["priority"], "high")
            self.assertEqual(updated.content["metadata"]["tags"], ["cleanup"])
            self.assertEqual(updated.content["metadata"]["assignee"], "alice")

    def test_runtime_rejects_invalid_task_status(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            item = runtime.create_task(thread, "Test")
            with self.assertRaisesRegex(ValueError, "invalid task status"):
                runtime.update_task(thread, item.id, status="invalid_status")

    def test_runtime_create_task_requires_subject(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            with self.assertRaisesRegex(ValueError, "task subject is required"):
                runtime.create_task(thread, "")

    def test_runtime_lists_tasks(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            t1 = runtime.create_task(thread, "Task one")
            t2 = runtime.create_task(thread, "Task two")
            t3 = runtime.create_task(thread, "Task three")
            runtime.update_task(thread, t3.id, status="completed")

            tasks = runtime.list_tasks(thread)

            self.assertEqual(len(tasks), 3)
            subjects = [t.content["subject"] for t in tasks]
            self.assertIn("Task one", subjects)
            self.assertIn("Task two", subjects)
            self.assertIn("Task three", subjects)

    def test_runtime_list_tasks_reflects_updates(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            item = runtime.create_task(thread, "Makes non-trivial")
            runtime.update_task(thread, item.id, status="in_progress")

            tasks = runtime.list_tasks(thread)

            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].content["status"], "in_progress")
            self.assertEqual(tasks[0].content["subject"], "Makes non-trivial")

    def test_runtime_updates_task_owner_and_blocks(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            item = runtime.create_task(thread, "Test", owner="alice")
            updated = runtime.update_task(
                thread, item.id, owner="bob", blocks=["step1"], blocked_by=["step0"]
            )
            tasks = runtime.list_tasks(thread)

            self.assertEqual(tasks[0].content["owner"], "bob")
            self.assertEqual(tasks[0].content["blocks"], ["step1"])
            self.assertEqual(tasks[0].content["blocked_by"], ["step0"])

    def test_runtime_records_budget_warning_when_context_exceeds_limit(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(
                store, StubProvider(), budget={"max_context_chars": 10}
            )
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "a long prompt that exceeds the tiny budget"))
            rows = store.read_thread(thread.id)

            routing = next(row for row in rows if row["kind"] == "routing_decision")
            warning = routing["payload"].get("budget_warning")
            self.assertIsNotNone(warning)
            self.assertIn("budget.max_context_chars", warning)
            self.assertIn("exceeds", warning)

    def test_runtime_no_budget_warning_when_within_limit(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(
                store, StubProvider(), budget={"max_context_chars": 1_000_000}
            )
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "short"))
            rows = store.read_thread(thread.id)

            routing = next(row for row in rows if row["kind"] == "routing_decision")
            warning = routing["payload"].get("budget_warning")
            self.assertIsNone(warning)

    def test_runtime_no_budget_warning_when_no_budget_configured(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)

            asyncio.run(runtime.run_user_turn(thread, "any text"))
            rows = store.read_thread(thread.id)

            routing = next(row for row in rows if row["kind"] == "routing_decision")
            warning = routing["payload"].get("budget_warning")
            self.assertIsNone(warning)

    def test_runtime_budget_warning_counts_workspace_context(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            (tmp_path / "AGENTS.md").write_text(
                "x" * 5000, encoding="utf-8"
            )
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(
                store, StubProvider(), budget={"max_context_chars": 100}
            )
            thread = runtime.start_thread(tmp_path)

            asyncio.run(
                runtime.run_user_turn(thread, "follow instructions")
            )
            rows = store.read_thread(thread.id)

            routing = next(row for row in rows if row["kind"] == "routing_decision")
            warning = routing["payload"].get("budget_warning")
            self.assertIsNotNone(warning)
            self.assertIn("budget.max_context_chars", warning or "")
            self.assertGreater(routing["payload"]["estimated_input_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
