from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from cocoa.workspace import WorkspaceScope


class ApprovalPrompter(Protocol):
    def approve(self, prompt: str) -> bool:
        raise NotImplementedError


class ConsoleApprovalPrompter:
    def approve(self, prompt: str) -> bool:
        answer = input(f"{prompt} [y/N] ").strip().lower()
        return answer in {"y", "yes"}


class AlwaysApprovePrompter:
    def approve(self, prompt: str) -> bool:
        return True


class AlwaysRejectPrompter:
    def approve(self, prompt: str) -> bool:
        return False


@dataclass(frozen=True)
class CommandResult:
    command: str
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    approved: bool = False


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    description: str
    read_only: bool = False
    side_effect: bool = False
    workspace_scope_required: bool = False
    requires_approval: bool = False
    path_required: bool = False


class Permission(Enum):
    ALLOW = "allow"
    REJECT = "reject"
    REQUIRES_APPROVAL = "requires_approval"


@dataclass(frozen=True)
class PermissionResult:
    decision: Permission
    reason: str = ""


class ToolPermissionPolicy:
    def __init__(self, tools: tuple[ToolDescriptor, ...]) -> None:
        self._tools_by_name = {t.name: t for t in tools}

    def check_tool_call(
        self,
        tool_name: str,
        path: str | None = None,
        scope: WorkspaceScope | None = None,
    ) -> PermissionResult:
        tool = self._tools_by_name.get(tool_name)
        if tool is None:
            return PermissionResult(
                Permission.REJECT, f"unknown tool: {tool_name}"
            )

        if tool.side_effect or tool.requires_approval:
            return PermissionResult(
                Permission.REQUIRES_APPROVAL,
                f"{tool_name} requires user approval",
            )

        if tool.workspace_scope_required:
            if scope is None:
                return PermissionResult(
                    Permission.REJECT,
                    f"{tool_name} requires workspace scope but none provided",
                )
            if tool.path_required and path is None:
                return PermissionResult(
                    Permission.REJECT,
                    f"{tool_name} requires a path argument",
                )
            if path is not None:
                try:
                    scope.resolve(path)
                except ValueError:
                    return PermissionResult(
                        Permission.REJECT,
                        f"path outside workspace: {path}",
                    )
                resolved = (scope.root / path).resolve()
                relative = resolved.relative_to(scope.root).as_posix()
                if scope.is_ignored(relative):
                    return PermissionResult(
                        Permission.REJECT,
                        f"path is ignored: {relative}",
                    )

        return PermissionResult(Permission.ALLOW, f"{tool_name} is allowed")


BUILTIN_TOOLS: tuple[ToolDescriptor, ...] = (
    ToolDescriptor(
        name="workspace_inspect",
        description="List and size files inside the workspace scope.",
        read_only=True,
        workspace_scope_required=True,
    ),
    ToolDescriptor(
        name="file_read",
        description="Read the content of a file inside the workspace scope.",
        read_only=True,
        workspace_scope_required=True,
        path_required=True,
    ),
    ToolDescriptor(
        name="shell_command",
        description="Propose a shell command for user approval and execution.",
        side_effect=True,
        requires_approval=True,
    ),
    ToolDescriptor(
        name="file_write",
        description="Propose to write or edit a file inside the workspace scope.",
        side_effect=True,
        workspace_scope_required=True,
        requires_approval=True,
        path_required=True,
    ),
    ToolDescriptor(
        name="file_edit",
        description="Propose an exact-text file edit inside the workspace scope.",
        side_effect=True,
        workspace_scope_required=True,
        requires_approval=True,
        path_required=True,
    ),
    ToolDescriptor(
        name="task_create",
        description="Create a new task item in the current thread.",
        side_effect=True,
    ),
    ToolDescriptor(
        name="task_get",
        description="Get a single task item by its id.",
        read_only=True,
    ),
    ToolDescriptor(
        name="task_update",
        description="Update the status or metadata of an existing task.",
        side_effect=True,
    ),
    ToolDescriptor(
        name="task_list",
        description="List all task items in the current thread.",
        read_only=True,
    ),
)


class ShellTool:
    def __init__(self, approval: ApprovalPrompter, timeout_seconds: int = 60) -> None:
        self.approval = approval
        self.timeout_seconds = timeout_seconds

    async def run(self, command: str, cwd: Path) -> CommandResult:
        approved = self.approval.approve(f"Run shell command: {command}")
        if not approved:
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=None,
                stdout="",
                stderr="Rejected by user.",
                approved=False,
            )

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            return CommandResult(
                command=command,
                cwd=str(cwd),
                exit_code=process.returncode,
                stdout=stdout_bytes.decode(errors="replace"),
                stderr=stderr_bytes.decode(errors="replace"),
                timed_out=True,
                approved=True,
            )

        return CommandResult(
            command=command,
            cwd=str(cwd),
            exit_code=process.returncode,
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
            approved=True,
        )
