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
        self.assertIn("file_edit", names)
        self.assertIn("task_create", names)
        self.assertIn("task_get", names)
        self.assertIn("task_update", names)
        self.assertIn("task_list", names)

    def test_tool_names_are_unique(self) -> None:
        names = [t.name for t in BUILTIN_TOOLS]
        self.assertEqual(len(names), len(set(names)))

    def test_tool_descriptor_has_required_fields(self) -> None:
        for tool in BUILTIN_TOOLS:
            self.assertIsInstance(tool.name, str)
            self.assertIsInstance(tool.description, str)
            self.assertIsInstance(tool.read_only, bool)
            self.assertIsInstance(tool.side_effect, bool)
            self.assertIsInstance(tool.workspace_scope_required, bool)
            self.assertIsInstance(tool.requires_approval, bool)
            self.assertTrue(tool.name)
            self.assertTrue(tool.description)

    def test_read_only_tools_have_no_side_effect(self) -> None:
        for tool in BUILTIN_TOOLS:
            if tool.read_only:
                self.assertFalse(tool.side_effect)
                self.assertFalse(tool.requires_approval)

    def test_side_effect_tools_are_not_read_only(self) -> None:
        for tool in BUILTIN_TOOLS:
            if tool.side_effect:
                self.assertFalse(tool.read_only)

    def test_side_effect_write_and_command_tools_require_approval(self) -> None:
        for tool in BUILTIN_TOOLS:
            if tool.side_effect and tool.workspace_scope_required:
                self.assertTrue(
                    tool.requires_approval,
                    f"side-effect workspace tool {tool.name} must set requires_approval=True",
                )

    def test_workspace_scope_tools_declare_scope(self) -> None:
        for tool in BUILTIN_TOOLS:
            if tool.workspace_scope_required:
                self.assertTrue(tool.read_only or tool.side_effect)

    def test_read_only_file_tools_require_scope(self) -> None:
        for tool in BUILTIN_TOOLS:
            if tool.name in ("workspace_inspect", "file_read"):
                self.assertTrue(tool.workspace_scope_required)
                self.assertFalse(tool.side_effect)
                self.assertFalse(tool.requires_approval)

    def test_task_create_is_side_effect(self) -> None:
        tool = next(t for t in BUILTIN_TOOLS if t.name == "task_create")
        self.assertTrue(tool.side_effect)
        self.assertFalse(tool.read_only)

    def test_task_get_is_read_only(self) -> None:
        tool = next(t for t in BUILTIN_TOOLS if t.name == "task_get")
        self.assertTrue(tool.read_only)
        self.assertFalse(tool.side_effect)

    def test_file_edit_has_correct_metadata(self) -> None:
        tool = next(t for t in BUILTIN_TOOLS if t.name == "file_edit")
        self.assertTrue(tool.side_effect)
        self.assertFalse(tool.read_only)
        self.assertTrue(tool.workspace_scope_required)
        self.assertTrue(tool.requires_approval)

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
