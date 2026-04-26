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
                    "item_completed",
                    "turn_completed",
                ],
            )

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


if __name__ == "__main__":
    unittest.main()
