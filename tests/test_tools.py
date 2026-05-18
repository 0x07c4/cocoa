from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cocoa.tools import (
    BUILTIN_TOOLS,
    Permission,
    ToolDescriptor,
    ToolPermissionPolicy,
)
from cocoa.workspace import WorkspaceScope


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
            self.assertIsInstance(tool.path_required, bool)
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
        file_read_tool = next(t for t in BUILTIN_TOOLS if t.name == "file_read")
        self.assertTrue(file_read_tool.path_required)
        workspace_inspect_tool = next(
            t for t in BUILTIN_TOOLS if t.name == "workspace_inspect"
        )
        self.assertFalse(workspace_inspect_tool.path_required)

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
        self.assertTrue(tool.path_required)

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


class ToolPermissionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = Path(__file__).resolve().parent / "_test_tmp_permissions"
        self._tmpdir.mkdir(exist_ok=True)
        (self._tmpdir / "readable.txt").write_text("hello")
        (self._tmpdir / ".git").mkdir(exist_ok=True)
        (self._tmpdir / ".git" / "HEAD").write_text("ref: main\n")
        self._scope = WorkspaceScope(self._tmpdir)
        self._policy = ToolPermissionPolicy(BUILTIN_TOOLS)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_unknown_tool_is_rejected(self) -> None:
        result = self._policy.check_tool_call("nonexistent_tool")
        self.assertEqual(result.decision, Permission.REJECT)

    def test_shell_command_requires_approval(self) -> None:
        result = self._policy.check_tool_call("shell_command")
        self.assertEqual(result.decision, Permission.REQUIRES_APPROVAL)

    def test_file_write_requires_approval(self) -> None:
        result = self._policy.check_tool_call("file_write")
        self.assertEqual(result.decision, Permission.REQUIRES_APPROVAL)

    def test_file_edit_requires_approval(self) -> None:
        result = self._policy.check_tool_call("file_edit")
        self.assertEqual(result.decision, Permission.REQUIRES_APPROVAL)

    def test_task_create_requires_approval(self) -> None:
        result = self._policy.check_tool_call("task_create")
        self.assertEqual(result.decision, Permission.REQUIRES_APPROVAL)

    def test_task_update_requires_approval(self) -> None:
        result = self._policy.check_tool_call("task_update")
        self.assertEqual(result.decision, Permission.REQUIRES_APPROVAL)

    def test_file_read_allowed_in_scope(self) -> None:
        result = self._policy.check_tool_call(
            "file_read", path="readable.txt", scope=self._scope
        )
        self.assertEqual(result.decision, Permission.ALLOW)

    def test_file_read_rejected_out_of_scope(self) -> None:
        result = self._policy.check_tool_call(
            "file_read", path="../outside.txt", scope=self._scope
        )
        self.assertEqual(result.decision, Permission.REJECT)

    def test_file_read_rejected_ignored_path(self) -> None:
        result = self._policy.check_tool_call(
            "file_read", path=".git/HEAD", scope=self._scope
        )
        self.assertEqual(result.decision, Permission.REJECT)

    def test_file_read_allowed_for_missing_in_scope_path(self) -> None:
        result = self._policy.check_tool_call(
            "file_read", path="missing.txt", scope=self._scope
        )
        self.assertEqual(result.decision, Permission.ALLOW)

    def test_file_read_rejected_no_path(self) -> None:
        result = self._policy.check_tool_call(
            "file_read", scope=self._scope
        )
        self.assertEqual(result.decision, Permission.REJECT)

    def test_workspace_inspect_allowed(self) -> None:
        result = self._policy.check_tool_call(
            "workspace_inspect", scope=self._scope
        )
        self.assertEqual(result.decision, Permission.ALLOW)

    def test_task_get_allowed(self) -> None:
        result = self._policy.check_tool_call("task_get")
        self.assertEqual(result.decision, Permission.ALLOW)

    def test_task_list_allowed(self) -> None:
        result = self._policy.check_tool_call("task_list")
        self.assertEqual(result.decision, Permission.ALLOW)

    def test_file_read_rejected_no_scope(self) -> None:
        result = self._policy.check_tool_call(
            "file_read", path="readable.txt", scope=None
        )
        self.assertEqual(result.decision, Permission.REJECT)

    def test_requires_approval_without_side_effect_returns_requires_approval(
        self,
    ) -> None:
        escalation_tool = ToolDescriptor(
            name="request_permissions",
            description="Explicit permission escalation request",
            requires_approval=True,
        )
        policy = ToolPermissionPolicy((escalation_tool,))
        result = policy.check_tool_call("request_permissions")
        self.assertEqual(result.decision, Permission.REQUIRES_APPROVAL)

    def test_policy_does_not_side_effect(self) -> None:
        before = sorted(p.name for p in self._tmpdir.iterdir())
        self._policy.check_tool_call(
            "file_read", path="readable.txt", scope=self._scope
        )
        self._policy.check_tool_call("shell_command")
        self._policy.check_tool_call("unknown")
        after = sorted(p.name for p in self._tmpdir.iterdir())
        self.assertEqual(before, after)

    def test_permission_result_has_reason(self) -> None:
        result = self._policy.check_tool_call("shell_command")
        self.assertIsInstance(result.reason, str)
        self.assertTrue(len(result.reason) > 0)


if __name__ == "__main__":
    unittest.main()
