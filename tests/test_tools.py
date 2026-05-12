from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocoa.tools import BUILTIN_TOOLS, ToolDescriptor


class ToolRegistryTests(unittest.TestCase):
    def test_builtin_tools_are_registered(self) -> None:
        names = [t.name for t in BUILTIN_TOOLS]
        self.assertIn("workspace_inspect", names)
        self.assertIn("file_read", names)
        self.assertIn("shell_command", names)
        self.assertIn("file_write", names)
        self.assertIn("task_create", names)
        self.assertIn("task_update", names)
        self.assertIn("task_list", names)

    def test_tool_descriptor_has_required_fields(self) -> None:
        for tool in BUILTIN_TOOLS:
            self.assertIsInstance(tool.name, str)
            self.assertIsInstance(tool.description, str)
            self.assertIsInstance(tool.read_only, bool)
            self.assertIsInstance(tool.side_effect, bool)
            self.assertTrue(tool.name)
            self.assertTrue(tool.description)

    def test_read_only_tools_have_no_side_effect(self) -> None:
        for tool in BUILTIN_TOOLS:
            if tool.read_only:
                self.assertFalse(tool.side_effect)

    def test_side_effect_tools_are_not_read_only(self) -> None:
        for tool in BUILTIN_TOOLS:
            if tool.side_effect:
                self.assertFalse(tool.read_only)

    def test_task_create_is_side_effect(self) -> None:
        tool = next(t for t in BUILTIN_TOOLS if t.name == "task_create")
        self.assertTrue(tool.side_effect)
        self.assertFalse(tool.read_only)

    def test_runtime_lists_registered_tools(self) -> None:
        from tempfile import TemporaryDirectory

        from cocoa.providers import StubProvider
        from cocoa.runtime import AgentRuntime
        from cocoa.store import JsonlStore

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            store = JsonlStore.for_workspace(tmp_path)
            runtime = AgentRuntime(store, StubProvider())

            tools = runtime.list_registered_tools()

            self.assertEqual(len(tools), len(BUILTIN_TOOLS))
            self.assertEqual(tools, BUILTIN_TOOLS)


if __name__ == "__main__":
    unittest.main()
