from __future__ import annotations

import asyncio
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
import unittest

from cocoa.models import (
    ApprovalState,
    EventKind,
    ItemKind,
    ItemRecord,
    ItemStatus,
    TurnRecord,
    event,
    new_id,
    now_ms,
)
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

    def test_projects_task_item_through_projection(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            thread_id = new_id("thr")
            turn_id = new_id("turn")
            item_id = new_id("item")
            store.append(
                event(
                    EventKind.THREAD_STARTED,
                    thread_id=thread_id,
                    payload={
                        "thread": {
                            "id": thread_id,
                            "cwd": str(tmp_path),
                            "status": "active",
                        }
                    },
                )
            )
            store.append(
                event(
                    EventKind.TURN_STARTED,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    payload={
                        "turn": {
                            "id": turn_id,
                            "thread_id": thread_id,
                            "intent": "task_create",
                            "status": "completed",
                        }
                    },
                )
            )
            task_item = ItemRecord(
                id=item_id,
                thread_id=thread_id,
                turn_id=turn_id,
                kind=ItemKind.TASK,
                status=ItemStatus.COMPLETED,
                content={
                    "subject": "Implement login",
                    "description": "Add user authentication",
                    "task_status": "pending",
                    "owner": "deepseek",
                },
            )
            store.append(
                event(
                    EventKind.ITEM_COMPLETED,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    payload={"item": task_item},
                )
            )

            view = load_thread_view(store, thread_id)

            self.assertEqual(len(view.turns), 1)
            self.assertEqual(len(view.turns[0].items), 1)
            item = view.turns[0].items[0]
            self.assertEqual(item.kind, "task")
            self.assertEqual(item.status, "completed")
            self.assertEqual(item.content["subject"], "Implement login")

    def test_projects_handoff_item_through_projection(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            thread_id = new_id("thr")
            turn_id = new_id("turn")
            item_id = new_id("item")
            store.append(
                event(
                    EventKind.THREAD_STARTED,
                    thread_id=thread_id,
                    payload={
                        "thread": {
                            "id": thread_id,
                            "cwd": str(tmp_path),
                            "status": "active",
                        }
                    },
                )
            )
            store.append(
                event(
                    EventKind.TURN_STARTED,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    payload={
                        "turn": {
                            "id": turn_id,
                            "thread_id": thread_id,
                            "intent": "handoff",
                            "status": "completed",
                        }
                    },
                )
            )
            handoff_item = ItemRecord(
                id=item_id,
                thread_id=thread_id,
                turn_id=turn_id,
                kind=ItemKind.HANDOFF,
                status=ItemStatus.COMPLETED,
                content={
                    "from": "codex",
                    "to": "deepseek",
                    "task_list": ["Task 1", "Task 2"],
                },
            )
            store.append(
                event(
                    EventKind.ITEM_COMPLETED,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    payload={"item": handoff_item},
                )
            )

            view = load_thread_view(store, thread_id)

            item = view.turns[0].items[0]
            self.assertEqual(item.kind, "handoff")
            self.assertEqual(item.content["from"], "codex")
            self.assertEqual(item.content["to"], "deepseek")

    def test_projects_review_item_through_projection(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            thread_id = new_id("thr")
            turn_id = new_id("turn")
            item_id = new_id("item")
            store.append(
                event(
                    EventKind.THREAD_STARTED,
                    thread_id=thread_id,
                    payload={
                        "thread": {
                            "id": thread_id,
                            "cwd": str(tmp_path),
                            "status": "active",
                        }
                    },
                )
            )
            store.append(
                event(
                    EventKind.TURN_STARTED,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    payload={
                        "turn": {
                            "id": turn_id,
                            "thread_id": thread_id,
                            "intent": "review",
                            "status": "completed",
                        }
                    },
                )
            )
            review_item = ItemRecord(
                id=item_id,
                thread_id=thread_id,
                turn_id=turn_id,
                kind=ItemKind.REVIEW,
                status=ItemStatus.COMPLETED,
                content={
                    "findings": ["LGTM"],
                    "score": 9,
                },
            )
            store.append(
                event(
                    EventKind.ITEM_COMPLETED,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    payload={"item": review_item},
                )
            )

            view = load_thread_view(store, thread_id)

            item = view.turns[0].items[0]
            self.assertEqual(item.kind, "review")
            self.assertEqual(item.content["findings"], ["LGTM"])
            self.assertEqual(item.content["score"], 9)

    def test_projection_keeps_latest_task_state_by_item_id(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            thread_id = new_id("thr")
            turn_id = new_id("turn")
            item_id = new_id("item")
            store.append(
                event(
                    EventKind.THREAD_STARTED,
                    thread_id=thread_id,
                    payload={
                        "thread": {
                            "id": thread_id,
                            "cwd": str(tmp_path),
                            "status": "active",
                        }
                    },
                )
            )
            store.append(
                event(
                    EventKind.TURN_STARTED,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    payload={
                        "turn": {
                            "id": turn_id,
                            "thread_id": thread_id,
                            "intent": "task_update",
                            "status": "completed",
                        }
                    },
                )
            )
            initial_item = ItemRecord(
                id=item_id,
                thread_id=thread_id,
                turn_id=turn_id,
                kind=ItemKind.TASK,
                status=ItemStatus.COMPLETED,
                content={
                    "subject": "Implement login",
                    "task_status": "pending",
                },
            )
            store.append(
                event(
                    EventKind.ITEM_COMPLETED,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    payload={"item": initial_item},
                )
            )
            updated_item = ItemRecord(
                id=item_id,
                thread_id=thread_id,
                turn_id=turn_id,
                kind=ItemKind.TASK,
                status=ItemStatus.COMPLETED,
                content={
                    "subject": "Implement login",
                    "task_status": "in_progress",
                },
            )
            store.append(
                event(
                    EventKind.ITEM_UPDATED,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    payload={"item": updated_item},
                )
            )

            view = load_thread_view(store, thread_id)

            self.assertEqual(len(view.turns[0].items), 1)
            item = view.turns[0].items[0]
            self.assertEqual(item.kind, "task")
            self.assertEqual(item.content["task_status"], "in_progress")

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
