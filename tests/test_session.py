from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocoa.session import SessionEngine
from cocoa.tools import AlwaysApprovePrompter, ShellTool


class SessionEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._existing_env = dict(os.environ)
        for key in list(os.environ):
            if key.startswith("COCOA_") or key.startswith("OPENAI_") or key.startswith("CODEX_"):
                del os.environ[key]
        # Point Codex home at a temp dir with no auth.json so auto-discovery yields stub
        self._fake_codex_home = f"/tmp/cocoa-test-{os.getpid()}"
        os.environ["COCOA_CODEX_HOME"] = str(self._fake_codex_home)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._existing_env)

    def _session(
        self, tmp_path: Path, overrides: dict[str, str] | None = None,
    ) -> SessionEngine:
        return SessionEngine(tmp_path, overrides=overrides)

    def test_session_starts_thread(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            session.start_thread(title="test")
            self.assertIsNotNone(session.thread)
            self.assertEqual(session.thread.title, "test")
            self.assertEqual(session.thread.cwd, str(tmp_path))

    def test_session_resumes_thread(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            session.start_thread(title="resume me")
            thread_id = session.thread.id

            session2 = self._session(tmp_path)
            session2.resume_thread(thread_id)
            self.assertEqual(session2.thread.id, thread_id)

    def test_session_set_override_rebuilds_runtime(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            session.set_override("COCOA_PROVIDER", "openai")
            self.assertIn("not configured", session.provider_status)

    def test_session_clear_overrides_works(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            session.set_override("COCOA_PROVIDER", "openai")
            self.assertIn("not configured", session.provider_status)
            session.clear_overrides()
            self.assertEqual(session.provider_status, "stub")

    def test_session_creates_and_lists_tasks(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            session.start_thread()
            t1 = session.create_task("Task A")
            t2 = session.create_task("Task B")

            tasks = session.list_tasks()
            subjects = [t.content["subject"] for t in tasks]
            self.assertIn("Task A", subjects)
            self.assertIn("Task B", subjects)

    def test_session_updates_task(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            session.start_thread()
            t = session.create_task("Test")
            updated = session.update_task(t.id, status="in_progress")
            self.assertEqual(updated.content["status"], "in_progress")

    def test_session_lists_registered_tools(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            tools = session.list_registered_tools()
            names = [t.name for t in tools]
            self.assertIn("workspace_inspect", names)
            self.assertIn("file_read", names)
            self.assertIn("shell_command", names)
            self.assertIn("file_write", names)
            self.assertIn("task_create", names)
            self.assertIn("task_update", names)
            self.assertIn("task_list", names)

    def test_session_load_view_and_thread_path(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            session.start_thread()
            view = session.load_view()
            self.assertEqual(view.id, session.thread.id)
            path = session.thread_path()
            self.assertTrue(path.endswith(".jsonl"))

    def test_session_env_and_mode_resolved(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            env = session.current_env()
            self.assertIn("COCOA_MODEL_MODE", env)
            self.assertEqual(session.resolved_mode(), "balanced")
            self.assertFalse(session.is_provider_configured())

    def test_session_run_user_turn_with_stub(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            session.start_thread()
            message = asyncio.run(session.run_user_turn("hello"))
            self.assertIn("Provider is not configured yet.", message)

    def test_session_overrides_copied_safely(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path, {"KEY": "val"})
            overrides = session.overrides
            self.assertEqual(overrides, {"KEY": "val"})
            overrides["EXTRA"] = "should not mutate"
            self.assertNotIn("EXTRA", session.overrides)

    def test_session_rejects_pending_proposal(self) -> None:
        from cocoa.models import (
            ApprovalState,
            EventKind,
            ItemKind,
            ItemRecord,
            ItemStatus,
            TurnRecord,
            TurnStatus,
            event,
            new_id,
            now_ms,
        )

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            session.start_thread()

            turn_id = new_id("turn")
            item_id = new_id("item")
            turn = TurnRecord(
                id=turn_id, thread_id=session.thread.id,
                intent="proposal", status=TurnStatus.COMPLETED,
            )
            session.store.append(
                event(EventKind.TURN_STARTED, thread_id=session.thread.id,
                      turn_id=turn_id, payload={"turn": turn})
            )
            proposal = ItemRecord(
                id=item_id, thread_id=session.thread.id,
                turn_id=turn_id, kind=ItemKind.COMMAND,
                status=ItemStatus.PENDING,
                content={"command": "echo test", "cwd": str(tmp_path)},
                approval=ApprovalState.REQUESTED,
            )
            session.store.append(
                event(EventKind.APPROVAL_REQUESTED, thread_id=session.thread.id,
                      turn_id=turn_id, item_id=item_id, payload={"item": proposal})
            )
            session.store.append(
                event(EventKind.ITEM_COMPLETED, thread_id=session.thread.id,
                      turn_id=turn_id, item_id=item_id, payload={"item": proposal})
            )

            rejected = session.reject_pending_item(item_id)

            self.assertEqual(rejected.status, "rejected")
            self.assertEqual(rejected.approval, "rejected")

    def test_session_applies_file_write_proposal(self) -> None:
        from cocoa.models import (
            ApprovalState,
            EventKind,
            ItemKind,
            ItemRecord,
            ItemStatus,
            TurnRecord,
            TurnStatus,
            event,
            new_id,
            now_ms,
        )

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            session.start_thread()

            turn_id = new_id("turn")
            item_id = new_id("item")
            turn = TurnRecord(
                id=turn_id, thread_id=session.thread.id,
                intent="proposal", status=TurnStatus.COMPLETED,
            )
            session.store.append(
                event(EventKind.TURN_STARTED, thread_id=session.thread.id,
                      turn_id=turn_id, payload={"turn": turn})
            )
            proposal = ItemRecord(
                id=item_id, thread_id=session.thread.id,
                turn_id=turn_id, kind=ItemKind.FILE_WRITE,
                status=ItemStatus.PENDING,
                content={
                    "operation": "write",
                    "path": "hello.txt",
                    "content": "hello from session\n",
                    "cwd": str(tmp_path),
                },
                approval=ApprovalState.REQUESTED,
            )
            session.store.append(
                event(EventKind.APPROVAL_REQUESTED, thread_id=session.thread.id,
                      turn_id=turn_id, item_id=item_id, payload={"item": proposal})
            )
            session.store.append(
                event(EventKind.ITEM_COMPLETED, thread_id=session.thread.id,
                      turn_id=turn_id, item_id=item_id, payload={"item": proposal})
            )

            applied = session.apply_proposed_file_write(item_id)

            self.assertEqual(applied.status, "completed")
            self.assertEqual(applied.approval, "accepted")
            self.assertTrue((tmp_path / "hello.txt").exists())
            self.assertEqual(
                (tmp_path / "hello.txt").read_text(encoding="utf-8"),
                "hello from session\n",
            )

    def test_session_runs_proposed_command(self) -> None:
        from cocoa.models import (
            ApprovalState,
            EventKind,
            ItemKind,
            ItemRecord,
            ItemStatus,
            TurnRecord,
            TurnStatus,
            event,
            new_id,
            now_ms,
        )

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            session = self._session(tmp_path)
            session.start_thread()

            session._shell = ShellTool(AlwaysApprovePrompter())

            turn_id = new_id("turn")
            item_id = new_id("item")
            turn = TurnRecord(
                id=turn_id, thread_id=session.thread.id,
                intent="proposal", status=TurnStatus.COMPLETED,
            )
            session.store.append(
                event(EventKind.TURN_STARTED, thread_id=session.thread.id,
                      turn_id=turn_id, payload={"turn": turn})
            )
            import shlex, sys
            cmd = f"{shlex.quote(sys.executable)} -c \"print('delegated')\""
            proposal = ItemRecord(
                id=item_id, thread_id=session.thread.id,
                turn_id=turn_id, kind=ItemKind.COMMAND,
                status=ItemStatus.PENDING,
                content={"command": cmd, "cwd": str(tmp_path)},
                approval=ApprovalState.REQUESTED,
            )
            session.store.append(
                event(EventKind.APPROVAL_REQUESTED, thread_id=session.thread.id,
                      turn_id=turn_id, item_id=item_id, payload={"item": proposal})
            )
            session.store.append(
                event(EventKind.ITEM_COMPLETED, thread_id=session.thread.id,
                      turn_id=turn_id, item_id=item_id, payload={"item": proposal})
            )

            result = asyncio.run(session.run_proposed_command(item_id))

            self.assertTrue(result.approved)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("delegated", result.stdout)


if __name__ == "__main__":
    unittest.main()
