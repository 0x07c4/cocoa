from pathlib import Path
import asyncio
import shlex
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocoa.providers import StubProvider
from cocoa.runtime import AgentRuntime
from cocoa.store import JsonlStore
from cocoa.tools import AlwaysApprovePrompter, AlwaysRejectPrompter, ShellTool


class FailingProvider:
    async def complete(self, request):
        raise RuntimeError("provider exploded")


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


if __name__ == "__main__":
    unittest.main()
