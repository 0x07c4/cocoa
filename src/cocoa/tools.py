from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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
