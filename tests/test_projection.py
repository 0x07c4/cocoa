from __future__ import annotations

import asyncio
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
import unittest

from cocoa.projection import load_thread_view, project_thread
from cocoa.providers import ProviderRequest, ProviderResponse, StubProvider
from cocoa.runtime import AgentRuntime
from cocoa.store import JsonlStore
from cocoa.tools import AlwaysApprovePrompter, AlwaysRejectPrompter, ShellTool


class FailingProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        raise RuntimeError("provider exploded")


class ProjectionTests(unittest.TestCase):
    def test_projects_completed_user_turn(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path, title="projection")
            asyncio.run(runtime.run_user_turn(thread, "hello"))

            view = load_thread_view(store, thread.id)

        self.assertEqual(view.id, thread.id)
        self.assertEqual(view.title, "projection")
        self.assertEqual(len(view.turns), 1)
        turn = view.turns[0]
        self.assertEqual(turn.status, "completed")
        self.assertEqual(turn.intent, "conversation")
        self.assertEqual(
            [item.kind for item in turn.items],
            ["user_message", "agent_message"],
        )
        self.assertEqual(turn.items[0].content["text"], "hello")
        self.assertIn("Provider is not configured yet.", turn.items[1].content["text"])

    def test_projects_provider_error_on_turn(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, FailingProvider())
            thread = runtime.start_thread(tmp_path)

            with self.assertRaisesRegex(RuntimeError, "provider exploded"):
                asyncio.run(runtime.run_user_turn(thread, "hello"))

            view = load_thread_view(store, thread.id)

        turn = view.turns[0]
        self.assertEqual(turn.status, "failed")
        self.assertEqual(turn.errors[0]["type"], "RuntimeError")
        self.assertEqual(turn.errors[0]["message"], "provider exploded")

    def test_projects_approved_shell_turn(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)
            shell = ShellTool(AlwaysApprovePrompter())
            command = f"{shlex.quote(sys.executable)} -c \"print('cocoa')\""

            asyncio.run(runtime.run_shell_turn(thread, command, shell))
            view = load_thread_view(store, thread.id)

        turn = view.turns[0]
        command_item = turn.items[-1]
        self.assertEqual(turn.status, "completed")
        self.assertEqual(command_item.kind, "command")
        self.assertEqual(command_item.status, "completed")
        self.assertEqual(command_item.approval, "accepted")
        self.assertEqual(command_item.content["exit_code"], 0)
        self.assertEqual(command_item.content["stdout"].strip(), "cocoa")

    def test_projects_rejected_shell_turn(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())
            thread = runtime.start_thread(tmp_path)
            shell = ShellTool(AlwaysRejectPrompter())

            asyncio.run(runtime.run_shell_turn(thread, "echo no", shell))
            view = load_thread_view(store, thread.id)

        turn = view.turns[0]
        command_item = turn.items[-1]
        self.assertEqual(turn.status, "interrupted")
        self.assertEqual(command_item.status, "rejected")
        self.assertEqual(command_item.approval, "rejected")

    def test_unknown_events_are_ignored(self) -> None:
        view = project_thread(
            [
                {
                    "kind": "thread_started",
                    "payload": {"thread": {"id": "thr_test", "cwd": "/tmp/work"}},
                },
                {"kind": "future_event", "payload": {"value": 1}},
            ]
        )

        self.assertEqual(view.id, "thr_test")
        self.assertEqual(view.turns, ())


if __name__ == "__main__":
    unittest.main()
