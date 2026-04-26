from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Any

from .models import (
    ApprovalState,
    EventKind,
    ThreadStatus,
    ItemKind,
    ItemRecord,
    ItemStatus,
    ThreadRecord,
    TurnRecord,
    TurnStatus,
    event,
    new_id,
    now_ms,
)
from .providers import ProviderAdapter, ProviderRequest
from .proposals import CommandProposal, FileEditProposal, FileWriteProposal, parse_proposals
from .store import JsonlStore
from .tools import CommandResult, ShellTool
from .workspace import WorkspaceScope


@dataclass(frozen=True)
class UserTurnResult:
    message: str
    context_items: tuple[ItemRecord, ...] = ()
    proposals: tuple[ItemRecord, ...] = ()


@dataclass(frozen=True)
class WorkspaceContext:
    text: str | None
    items: tuple[ItemRecord, ...] = ()


_PATH_REFERENCE_RE = re.compile(
    r"(?<![\w@])@(?P<path>[A-Za-z0-9][A-Za-z0-9._/\-]*)(?=$|[\s,.;:!?)}\]])"
)
_MAX_WORKSPACE_MAP_ENTRIES = 80
_MAX_CONTEXT_REFERENCES = 6
_MAX_CONTEXT_FILE_BYTES = 32_000
_MAX_WORKSPACE_CONTEXT_CHARS = 90_000


class AgentRuntime:
    def __init__(self, store: JsonlStore, provider: ProviderAdapter) -> None:
        self.store = store
        self.provider = provider

    def start_thread(self, cwd: Path, title: str | None = None) -> ThreadRecord:
        thread = ThreadRecord(id=new_id("thr"), cwd=str(cwd.resolve()), title=title)
        self.store.append(
            event(
                EventKind.THREAD_STARTED,
                thread_id=thread.id,
                payload={"thread": thread},
            )
        )
        return thread

    def resume_thread(self, thread_id: str) -> ThreadRecord:
        self.store.thread_path(thread_id)
        rows = self.store.read_thread(thread_id)
        if not rows:
            raise ValueError(f"thread not found: {thread_id}")

        for row in rows:
            if row.get("kind") != "thread_started":
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            raw_thread = payload.get("thread")
            if not isinstance(raw_thread, dict):
                continue
            return self._build_thread_from_payload(raw_thread)

        raise ValueError(f"thread started event missing for thread: {thread_id}")

    def start_turn(self, thread: ThreadRecord, intent: str, user_text: str) -> TurnRecord:
        turn = TurnRecord(
            id=new_id("turn"),
            thread_id=thread.id,
            intent=intent,
            status=TurnStatus.RUNNING,
        )
        user_item = ItemRecord(
            id=new_id("item"),
            thread_id=thread.id,
            turn_id=turn.id,
            kind=ItemKind.USER_MESSAGE,
            status=ItemStatus.COMPLETED,
            content={"text": user_text},
            completed_at_ms=now_ms(),
        )
        self.store.append_many(
            [
                event(
                    EventKind.TURN_STARTED,
                    thread_id=thread.id,
                    turn_id=turn.id,
                    payload={"turn": turn},
                ),
                event(
                    EventKind.ITEM_COMPLETED,
                    thread_id=thread.id,
                    turn_id=turn.id,
                    item_id=user_item.id,
                    payload={"item": user_item},
                ),
            ]
        )
        return turn

    def complete_turn(
        self,
        thread: ThreadRecord,
        turn: TurnRecord,
        *,
        status: TurnStatus,
        summary: str | None = None,
    ) -> TurnRecord:
        completed = TurnRecord(
            id=turn.id,
            thread_id=thread.id,
            intent=turn.intent,
            status=status,
            created_at_ms=turn.created_at_ms,
            completed_at_ms=now_ms(),
            summary=summary,
        )
        self.store.append(
            event(
                EventKind.TURN_COMPLETED,
                thread_id=thread.id,
                turn_id=turn.id,
                payload={"turn": completed},
            )
        )
        return completed

    async def run_user_turn(self, thread: ThreadRecord, prompt: str) -> str:
        result = await self.run_user_turn_with_result(thread, prompt)
        return result.message

    async def run_user_turn_with_result(
        self,
        thread: ThreadRecord,
        prompt: str,
    ) -> UserTurnResult:
        turn = self.start_turn(thread, intent="conversation", user_text=prompt)
        context = self._build_thread_context(thread, skip_turn_id=turn.id)
        workspace_context = self._build_workspace_context(thread, turn, prompt)
        if workspace_context.items:
            self.store.append_many(
                event(
                    EventKind.ITEM_COMPLETED,
                    thread_id=thread.id,
                    turn_id=turn.id,
                    item_id=item.id,
                    payload={"item": item},
                )
                for item in workspace_context.items
            )
        try:
            response = await self.provider.complete(
                ProviderRequest(
                    thread_id=thread.id,
                    turn_id=turn.id,
                    prompt=prompt,
                    cwd=thread.cwd,
                    thread_context=context,
                    workspace_context=workspace_context.text,
                )
            )
        except Exception as exc:
            self.store.append(
                event(
                    EventKind.ERROR,
                    thread_id=thread.id,
                    turn_id=turn.id,
                    payload={"message": str(exc), "type": type(exc).__name__},
                )
            )
            self.complete_turn(
                thread,
                turn,
                status=TurnStatus.FAILED,
                summary=f"provider error: {type(exc).__name__}",
            )
            raise

        parsed = parse_proposals(response.message)
        message = parsed.message
        if not message:
            message = response.message.strip()

        agent_item = ItemRecord(
            id=new_id("item"),
            thread_id=thread.id,
            turn_id=turn.id,
            kind=ItemKind.AGENT_MESSAGE,
            status=ItemStatus.COMPLETED,
            content={"text": message},
            completed_at_ms=now_ms(),
        )
        events = [
            event(
                EventKind.ITEM_COMPLETED,
                thread_id=thread.id,
                turn_id=turn.id,
                item_id=agent_item.id,
                payload={"item": agent_item},
            )
        ]
        proposal_items_list: list[ItemRecord] = []
        proposal_items_list.extend(
            self._command_proposal_item(thread, turn, proposal)
            for proposal in parsed.commands
        )
        proposal_items_list.extend(
            self._file_write_proposal_item(thread, turn, proposal)
            for proposal in parsed.file_writes
        )
        proposal_items_list.extend(
            self._file_edit_proposal_item(thread, turn, proposal)
            for proposal in parsed.file_edits
        )
        proposal_items = tuple(proposal_items_list)
        for item in proposal_items:
            events.append(
                event(
                    EventKind.APPROVAL_REQUESTED,
                    thread_id=thread.id,
                    turn_id=turn.id,
                    item_id=item.id,
                    payload={"item": item},
                )
            )
        self.store.append_many(events)
        self.complete_turn(
            thread,
            turn,
            status=TurnStatus.COMPLETED,
            summary=response.summary,
        )
        return UserTurnResult(
            message=message,
            context_items=workspace_context.items,
            proposals=proposal_items,
        )

    async def run_shell_turn(
        self,
        thread: ThreadRecord,
        command: str,
        shell: ShellTool,
    ) -> CommandResult:
        turn = self.start_turn(thread, intent="manual_command", user_text=f"/run {command}")
        pending_item = ItemRecord(
            id=new_id("item"),
            thread_id=thread.id,
            turn_id=turn.id,
            kind=ItemKind.COMMAND,
            status=ItemStatus.PENDING,
            content={"command": command, "cwd": thread.cwd},
            approval=ApprovalState.REQUESTED,
        )
        self.store.append_many(
            [
                event(
                    EventKind.APPROVAL_REQUESTED,
                    thread_id=thread.id,
                    turn_id=turn.id,
                    item_id=pending_item.id,
                    payload={"item": pending_item},
                ),
            ]
        )
        try:
            result = await shell.run(command, Path(thread.cwd))
        except Exception as exc:
            failed_item = ItemRecord(
                id=pending_item.id,
                thread_id=thread.id,
                turn_id=turn.id,
                kind=ItemKind.COMMAND,
                status=ItemStatus.FAILED,
                content={"command": command, "cwd": thread.cwd, "error": str(exc)},
                approval=ApprovalState.REQUESTED,
                completed_at_ms=now_ms(),
            )
            self.store.append_many(
                [
                    event(
                        EventKind.ERROR,
                        thread_id=thread.id,
                        turn_id=turn.id,
                        item_id=pending_item.id,
                        payload={"message": str(exc), "type": type(exc).__name__},
                    ),
                    event(
                        EventKind.ITEM_COMPLETED,
                        thread_id=thread.id,
                        turn_id=turn.id,
                        item_id=failed_item.id,
                        payload={"item": failed_item},
                    ),
                ]
            )
            self.complete_turn(
                thread,
                turn,
                status=TurnStatus.FAILED,
                summary=f"shell error: {type(exc).__name__}",
            )
            raise

        command_succeeded = result.approved and result.exit_code == 0 and not result.timed_out
        final_item = ItemRecord(
            id=pending_item.id,
            thread_id=thread.id,
            turn_id=turn.id,
            kind=ItemKind.COMMAND,
            status=(
                ItemStatus.COMPLETED
                if command_succeeded
                else ItemStatus.FAILED
                if result.approved
                else ItemStatus.REJECTED
            ),
            content={
                "command": command,
                "cwd": result.cwd,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
            },
            approval=ApprovalState.ACCEPTED if result.approved else ApprovalState.REJECTED,
            completed_at_ms=now_ms(),
        )
        self.store.append_many(
            [
                event(
                    EventKind.APPROVAL_RESOLVED,
                    thread_id=thread.id,
                    turn_id=turn.id,
                    item_id=final_item.id,
                    payload={"approved": result.approved},
                ),
                event(
                    EventKind.ITEM_COMPLETED,
                    thread_id=thread.id,
                    turn_id=turn.id,
                    item_id=final_item.id,
                    payload={"item": final_item},
                ),
            ]
        )
        self.complete_turn(
            thread,
            turn,
            status=(
                TurnStatus.COMPLETED
                if command_succeeded
                else TurnStatus.FAILED
                if result.approved
                else TurnStatus.INTERRUPTED
            ),
            summary=f"command: {command}",
        )
        return result

    async def run_proposed_command(
        self,
        thread: ThreadRecord,
        item_id: str,
        shell: ShellTool,
    ) -> CommandResult:
        pending_item = self._find_pending_command(thread, item_id)
        command = pending_item.content.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"invalid command proposal: {item_id}")
        try:
            result = await shell.run(command, Path(thread.cwd))
        except Exception as exc:
            failed_item = ItemRecord(
                id=pending_item.id,
                thread_id=thread.id,
                turn_id=pending_item.turn_id,
                kind=ItemKind.COMMAND,
                status=ItemStatus.FAILED,
                content={**pending_item.content, "error": str(exc)},
                approval=ApprovalState.REQUESTED,
                created_at_ms=pending_item.created_at_ms,
                completed_at_ms=now_ms(),
            )
            self.store.append_many(
                [
                    event(
                        EventKind.ERROR,
                        thread_id=thread.id,
                        turn_id=pending_item.turn_id,
                        item_id=pending_item.id,
                        payload={"message": str(exc), "type": type(exc).__name__},
                    ),
                    event(
                        EventKind.ITEM_COMPLETED,
                        thread_id=thread.id,
                        turn_id=pending_item.turn_id,
                        item_id=failed_item.id,
                        payload={"item": failed_item},
                    ),
                ]
            )
            raise

        command_succeeded = result.approved and result.exit_code == 0 and not result.timed_out
        final_item = ItemRecord(
            id=pending_item.id,
            thread_id=thread.id,
            turn_id=pending_item.turn_id,
            kind=ItemKind.COMMAND,
            status=(
                ItemStatus.COMPLETED
                if command_succeeded
                else ItemStatus.FAILED
                if result.approved
                else ItemStatus.REJECTED
            ),
            content={
                **pending_item.content,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
            },
            approval=ApprovalState.ACCEPTED if result.approved else ApprovalState.REJECTED,
            created_at_ms=pending_item.created_at_ms,
            completed_at_ms=now_ms(),
        )
        self.store.append_many(
            [
                event(
                    EventKind.APPROVAL_RESOLVED,
                    thread_id=thread.id,
                    turn_id=pending_item.turn_id,
                    item_id=final_item.id,
                    payload={"approved": result.approved},
                ),
                event(
                    EventKind.ITEM_COMPLETED,
                    thread_id=thread.id,
                    turn_id=pending_item.turn_id,
                    item_id=final_item.id,
                    payload={"item": final_item},
                ),
            ]
        )
        return result

    def apply_proposed_file_write(
        self,
        thread: ThreadRecord,
        item_id: str,
    ) -> ItemRecord:
        pending_item = self._find_pending_file_write(thread, item_id)
        operation = pending_item.content.get("operation", "write")
        if operation == "replace":
            return self._apply_proposed_file_edit(thread, pending_item)
        if operation != "write":
            raise ValueError(f"unsupported file write operation: {operation}")
        path = pending_item.content.get("path")
        content = pending_item.content.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise ValueError(f"invalid file write proposal: {item_id}")

        target, relative = self._resolve_workspace_write_path(thread, path)
        previous_exists = target.exists()
        previous_size = target.stat().st_size if previous_exists else 0
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        final_item = ItemRecord(
            id=pending_item.id,
            thread_id=thread.id,
            turn_id=pending_item.turn_id,
            kind=ItemKind.FILE_WRITE,
            status=ItemStatus.COMPLETED,
            content={
                **pending_item.content,
                "path": relative,
                "previous_exists": previous_exists,
                "previous_size": previous_size,
                "bytes_written": len(content.encode("utf-8")),
            },
            approval=ApprovalState.ACCEPTED,
            created_at_ms=pending_item.created_at_ms,
            completed_at_ms=now_ms(),
        )
        self.store.append_many(
            [
                event(
                    EventKind.APPROVAL_RESOLVED,
                    thread_id=thread.id,
                    turn_id=pending_item.turn_id,
                    item_id=final_item.id,
                    payload={"approved": True},
                ),
                event(
                    EventKind.ITEM_COMPLETED,
                    thread_id=thread.id,
                    turn_id=pending_item.turn_id,
                    item_id=final_item.id,
                    payload={"item": final_item},
                ),
            ]
        )
        return final_item

    def _apply_proposed_file_edit(
        self,
        thread: ThreadRecord,
        pending_item: ItemRecord,
    ) -> ItemRecord:
        path = pending_item.content.get("path")
        old = pending_item.content.get("old")
        new = pending_item.content.get("new")
        replace_all = pending_item.content.get("replace_all") is True
        if not isinstance(path, str) or not isinstance(old, str) or not isinstance(new, str):
            raise ValueError(f"invalid file edit proposal: {pending_item.id}")
        target, relative = self._resolve_workspace_write_path(thread, path)
        if not target.exists():
            raise ValueError(f"path does not exist: {relative}")
        previous = target.read_text(encoding="utf-8")
        updated = self._replace_exact_text(
            previous,
            old,
            new,
            replace_all=replace_all,
            item_id=pending_item.id,
        )
        previous_size = target.stat().st_size
        target.write_text(updated, encoding="utf-8")
        final_item = ItemRecord(
            id=pending_item.id,
            thread_id=thread.id,
            turn_id=pending_item.turn_id,
            kind=ItemKind.FILE_WRITE,
            status=ItemStatus.COMPLETED,
            content={
                **pending_item.content,
                "path": relative,
                "previous_exists": True,
                "previous_size": previous_size,
                "bytes_written": len(updated.encode("utf-8")),
            },
            approval=ApprovalState.ACCEPTED,
            created_at_ms=pending_item.created_at_ms,
            completed_at_ms=now_ms(),
        )
        self.store.append_many(
            [
                event(
                    EventKind.APPROVAL_RESOLVED,
                    thread_id=thread.id,
                    turn_id=pending_item.turn_id,
                    item_id=final_item.id,
                    payload={"approved": True},
                ),
                event(
                    EventKind.ITEM_COMPLETED,
                    thread_id=thread.id,
                    turn_id=pending_item.turn_id,
                    item_id=final_item.id,
                    payload={"item": final_item},
                ),
            ]
        )
        return final_item

    def reject_pending_item(
        self,
        thread: ThreadRecord,
        item_id: str,
    ) -> ItemRecord:
        pending_item = self._find_pending_proposal(thread, item_id)
        final_item = ItemRecord(
            id=pending_item.id,
            thread_id=thread.id,
            turn_id=pending_item.turn_id,
            kind=pending_item.kind,
            status=ItemStatus.REJECTED,
            content=pending_item.content,
            approval=ApprovalState.REJECTED,
            created_at_ms=pending_item.created_at_ms,
            completed_at_ms=now_ms(),
        )
        self.store.append_many(
            [
                event(
                    EventKind.APPROVAL_RESOLVED,
                    thread_id=thread.id,
                    turn_id=pending_item.turn_id,
                    item_id=final_item.id,
                    payload={"approved": False},
                ),
                event(
                    EventKind.ITEM_COMPLETED,
                    thread_id=thread.id,
                    turn_id=pending_item.turn_id,
                    item_id=final_item.id,
                    payload={"item": final_item},
                ),
            ]
        )
        return final_item

    def _command_proposal_item(
        self,
        thread: ThreadRecord,
        turn: TurnRecord,
        proposal: CommandProposal,
    ) -> ItemRecord:
        content: dict[str, Any] = {
            "command": proposal.command,
            "cwd": thread.cwd,
            "source": "provider_proposal",
        }
        if proposal.reason:
            content["reason"] = proposal.reason
        return ItemRecord(
            id=new_id("item"),
            thread_id=thread.id,
            turn_id=turn.id,
            kind=ItemKind.COMMAND,
            status=ItemStatus.PENDING,
            content=content,
            approval=ApprovalState.REQUESTED,
        )

    def _file_write_proposal_item(
        self,
        thread: ThreadRecord,
        turn: TurnRecord,
        proposal: FileWriteProposal,
    ) -> ItemRecord:
        content: dict[str, Any] = {
            "operation": "write",
            "path": proposal.path,
            "content": proposal.content,
            "source": "provider_proposal",
        }
        if proposal.reason:
            content["reason"] = proposal.reason
        try:
            target, relative = self._resolve_workspace_write_path(thread, proposal.path)
            content["path"] = relative
            content["diff"] = self._file_write_diff(target, relative, proposal.content)
        except ValueError as exc:
            content["scope_error"] = str(exc)
        except (OSError, UnicodeError) as exc:
            content["scope_error"] = str(exc)
        return ItemRecord(
            id=new_id("item"),
            thread_id=thread.id,
            turn_id=turn.id,
            kind=ItemKind.FILE_WRITE,
            status=ItemStatus.PENDING,
            content=content,
            approval=ApprovalState.REQUESTED,
        )

    def _file_edit_proposal_item(
        self,
        thread: ThreadRecord,
        turn: TurnRecord,
        proposal: FileEditProposal,
    ) -> ItemRecord:
        content: dict[str, Any] = {
            "operation": "replace",
            "path": proposal.path,
            "old": proposal.old,
            "new": proposal.new,
            "replace_all": proposal.replace_all,
            "source": "provider_proposal",
        }
        if proposal.reason:
            content["reason"] = proposal.reason
        try:
            target, relative = self._resolve_workspace_write_path(thread, proposal.path)
            content["path"] = relative
            content["diff"] = self._file_edit_diff(
                target,
                relative,
                proposal.old,
                proposal.new,
                replace_all=proposal.replace_all,
            )
        except ValueError as exc:
            content["scope_error"] = str(exc)
        except (OSError, UnicodeError) as exc:
            content["scope_error"] = str(exc)
        return ItemRecord(
            id=new_id("item"),
            thread_id=thread.id,
            turn_id=turn.id,
            kind=ItemKind.FILE_WRITE,
            status=ItemStatus.PENDING,
            content=content,
            approval=ApprovalState.REQUESTED,
        )

    def _build_workspace_context(
        self,
        thread: ThreadRecord,
        turn: TurnRecord,
        prompt: str,
    ) -> WorkspaceContext:
        scope = WorkspaceScope(Path(thread.cwd))
        sections: list[str] = []
        items: list[ItemRecord] = []

        workspace_map = self._workspace_map_context(scope)
        if workspace_map:
            sections.append(workspace_map)

        references = self._extract_path_references(prompt)
        if references:
            reference_sections = ["Referenced workspace paths:"]
            for raw_path in references[:_MAX_CONTEXT_REFERENCES]:
                item, context_text = self._referenced_path_context_item(
                    thread,
                    turn,
                    scope,
                    raw_path,
                )
                items.append(item)
                reference_sections.append(context_text)
            skipped = len(references) - _MAX_CONTEXT_REFERENCES
            if skipped > 0:
                reference_sections.append(
                    f"- skipped {skipped} additional @path reference(s)"
                )
            sections.append("\n\n".join(reference_sections))

        text = "\n\n".join(section for section in sections if section.strip()).strip()
        if not text:
            return WorkspaceContext(text=None, items=tuple(items))
        if len(text) > _MAX_WORKSPACE_CONTEXT_CHARS:
            text = (
                text[:_MAX_WORKSPACE_CONTEXT_CHARS]
                + "\n\n[workspace context truncated by cocoa]"
            )
        return WorkspaceContext(text=text, items=tuple(items))

    def _workspace_map_context(self, scope: WorkspaceScope) -> str | None:
        try:
            entries = scope.inspect(".", max_entries=_MAX_WORKSPACE_MAP_ENTRIES)
        except (OSError, ValueError) as exc:
            return f"Workspace file map unavailable: {exc}"
        if not entries:
            return "Workspace file map: empty workspace"
        lines = [
            f"Workspace file map (first {len(entries)} visible entries; ignored paths omitted):"
        ]
        for entry in entries:
            lines.append(f"- {entry.path} ({entry.size} bytes)")
        return "\n".join(lines)

    def _extract_path_references(self, prompt: str) -> tuple[str, ...]:
        seen: set[str] = set()
        references: list[str] = []
        for match in _PATH_REFERENCE_RE.finditer(prompt):
            path = match.group("path").strip()
            if not path or path in seen:
                continue
            seen.add(path)
            references.append(path)
        return tuple(references)

    def _referenced_path_context_item(
        self,
        thread: ThreadRecord,
        turn: TurnRecord,
        scope: WorkspaceScope,
        raw_path: str,
    ) -> tuple[ItemRecord, str]:
        try:
            target = scope.resolve(raw_path)
            relative = target.relative_to(scope.root).as_posix()
            if scope.is_ignored(relative):
                raise ValueError(f"path is ignored: {relative}")
            if not target.exists():
                raise FileNotFoundError(relative)
            if target.is_dir():
                return self._referenced_directory_context_item(
                    thread,
                    turn,
                    scope,
                    relative,
                )
            if target.is_file():
                return self._referenced_file_context_item(
                    thread,
                    turn,
                    target,
                    relative,
                )
            raise ValueError(f"path is not a regular file or directory: {relative}")
        except (OSError, UnicodeError, ValueError) as exc:
            item = ItemRecord(
                id=new_id("item"),
                thread_id=thread.id,
                turn_id=turn.id,
                kind=ItemKind.FILE_READ,
                status=ItemStatus.FAILED,
                content={
                    "path": raw_path,
                    "source": "prompt_reference",
                    "error": str(exc),
                },
                completed_at_ms=now_ms(),
            )
            return item, f"- @{raw_path}: unavailable ({exc})"

    def _referenced_file_context_item(
        self,
        thread: ThreadRecord,
        turn: TurnRecord,
        target: Path,
        relative: str,
    ) -> tuple[ItemRecord, str]:
        size = target.stat().st_size
        with target.open("rb") as handle:
            raw = handle.read(_MAX_CONTEXT_FILE_BYTES + 1)
        if b"\x00" in raw:
            raise UnicodeError(f"file appears to be binary: {relative}")
        truncated = len(raw) > _MAX_CONTEXT_FILE_BYTES
        text = raw[:_MAX_CONTEXT_FILE_BYTES].decode("utf-8", errors="replace")
        item = ItemRecord(
            id=new_id("item"),
            thread_id=thread.id,
            turn_id=turn.id,
            kind=ItemKind.FILE_READ,
            status=ItemStatus.COMPLETED,
            content={
                "path": relative,
                "size": size,
                "truncated": truncated,
                "source": "prompt_reference",
                "text": text,
            },
            completed_at_ms=now_ms(),
        )
        header = f"@{relative} ({size} bytes"
        if truncated:
            header += f", first {_MAX_CONTEXT_FILE_BYTES} bytes"
        header += ")"
        context_text = f"{header}\n<file path=\"{relative}\">\n{text}\n</file>"
        return item, context_text

    def _referenced_directory_context_item(
        self,
        thread: ThreadRecord,
        turn: TurnRecord,
        scope: WorkspaceScope,
        relative: str,
    ) -> tuple[ItemRecord, str]:
        entries = scope.inspect(relative, max_entries=_MAX_WORKSPACE_MAP_ENTRIES)
        payload_entries = [{"path": entry.path, "size": entry.size} for entry in entries]
        item = ItemRecord(
            id=new_id("item"),
            thread_id=thread.id,
            turn_id=turn.id,
            kind=ItemKind.WORKSPACE_INSPECT,
            status=ItemStatus.COMPLETED,
            content={
                "path": relative,
                "entries": payload_entries,
                "source": "prompt_reference",
            },
            completed_at_ms=now_ms(),
        )
        lines = [f"@{relative}/ directory listing:"]
        for entry in entries:
            lines.append(f"- {entry.path} ({entry.size} bytes)")
        if len(entries) >= _MAX_WORKSPACE_MAP_ENTRIES:
            lines.append(f"- ... capped at {_MAX_WORKSPACE_MAP_ENTRIES} entries")
        return item, "\n".join(lines)

    def _find_pending_command(self, thread: ThreadRecord, item_id: str) -> ItemRecord:
        return self._find_pending_item(
            thread,
            item_id,
            kind=ItemKind.COMMAND,
            missing_message="command proposal not found",
            wrong_kind_message="item is not a command proposal",
        )

    def _find_pending_file_write(self, thread: ThreadRecord, item_id: str) -> ItemRecord:
        return self._find_pending_item(
            thread,
            item_id,
            kind=ItemKind.FILE_WRITE,
            missing_message="file write proposal not found",
            wrong_kind_message="item is not a file write proposal",
        )

    def _find_pending_proposal(self, thread: ThreadRecord, item_id: str) -> ItemRecord:
        item = self._find_latest_item_payload(thread, item_id)
        if item is None:
            raise ValueError(f"pending proposal not found: {item_id}")
        raw_kind = item.get("kind")
        if raw_kind not in {ItemKind.COMMAND.value, ItemKind.FILE_WRITE.value}:
            raise ValueError(f"item is not a proposal: {item_id}")
        return self._pending_item_from_payload(
            thread,
            item_id,
            item,
            kind=ItemKind(raw_kind),
        )

    def _find_pending_item(
        self,
        thread: ThreadRecord,
        item_id: str,
        *,
        kind: ItemKind,
        missing_message: str,
        wrong_kind_message: str,
    ) -> ItemRecord:
        item = self._find_latest_item_payload(thread, item_id)
        if item is None:
            raise ValueError(f"{missing_message}: {item_id}")
        if item.get("kind") != kind.value:
            raise ValueError(f"{wrong_kind_message}: {item_id}")
        return self._pending_item_from_payload(thread, item_id, item, kind=kind)

    def _find_latest_item_payload(
        self,
        thread: ThreadRecord,
        item_id: str,
    ) -> dict[str, Any] | None:
        item: dict[str, Any] | None = None
        for row in self.store.read_thread(thread.id):
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            raw_item = payload.get("item")
            if not isinstance(raw_item, dict) or raw_item.get("id") != item_id:
                continue
            item = raw_item
        return item

    def _pending_item_from_payload(
        self,
        thread: ThreadRecord,
        item_id: str,
        item: dict[str, Any],
        *,
        kind: ItemKind,
    ) -> ItemRecord:
        if item.get("status") != ItemStatus.PENDING.value:
            raise ValueError(f"{kind.value} proposal is not pending: {item_id}")
        if item.get("approval") != ApprovalState.REQUESTED.value:
            raise ValueError(f"{kind.value} proposal is not awaiting approval: {item_id}")
        raw_content = item.get("content")
        content = dict(raw_content) if isinstance(raw_content, dict) else {}
        created_at_ms = item.get("created_at_ms")
        if not isinstance(created_at_ms, int):
            created_at_ms = now_ms()
        return ItemRecord(
            id=item_id,
            thread_id=thread.id,
            turn_id=str(item.get("turn_id")),
            kind=kind,
            status=ItemStatus.PENDING,
            content=content,
            approval=ApprovalState.REQUESTED,
            created_at_ms=created_at_ms,
        )

    def _resolve_workspace_write_path(
        self,
        thread: ThreadRecord,
        path: str,
    ) -> tuple[Path, str]:
        scope = WorkspaceScope(Path(thread.cwd))
        target = scope.resolve(path)
        relative = target.relative_to(scope.root).as_posix()
        if relative == ".":
            raise ValueError("path must be a file inside workspace")
        if scope.is_ignored(relative):
            raise ValueError(f"path is ignored: {relative}")
        if target.exists() and target.is_dir():
            raise ValueError(f"path is a directory: {relative}")
        return target, relative

    def _file_write_diff(self, target: Path, relative: str, content: str) -> str:
        if target.exists():
            old = target.read_text(encoding="utf-8")
            fromfile = f"a/{relative}"
        else:
            old = ""
            fromfile = "/dev/null"
        return "".join(
            unified_diff(
                old.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=fromfile,
                tofile=f"b/{relative}",
            )
        )

    def _file_edit_diff(
        self,
        target: Path,
        relative: str,
        old: str,
        new: str,
        *,
        replace_all: bool,
    ) -> str:
        if not target.exists():
            raise ValueError(f"path does not exist: {relative}")
        current = target.read_text(encoding="utf-8")
        updated = self._replace_exact_text(
            current,
            old,
            new,
            replace_all=replace_all,
            item_id=relative,
        )
        return "".join(
            unified_diff(
                current.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )

    def _replace_exact_text(
        self,
        current: str,
        old: str,
        new: str,
        *,
        replace_all: bool,
        item_id: str,
    ) -> str:
        if not old:
            raise ValueError(f"file edit proposal has empty old text: {item_id}")
        count = current.count(old)
        if count == 0:
            raise ValueError(f"old text not found for file edit proposal: {item_id}")
        if count > 1 and not replace_all:
            raise ValueError(
                f"old text matched {count} times; set replace_all=true or make it unique: {item_id}"
            )
        limit = -1 if replace_all else 1
        return current.replace(old, new, limit)

    def _build_thread_context(
        self,
        thread: ThreadRecord,
        *,
        max_turns: int = 6,
        skip_turn_id: str,
    ) -> str | None:
        pairs: list[tuple[str, str]] = []
        turns: dict[str, dict[str, str]] = {}
        turn_order: list[str] = []

        for row in self.store.read_thread(thread.id):
            if row.get("kind") != "item_completed":
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            item = payload.get("item")
            if not isinstance(item, dict):
                continue
            turn_id = item.get("turn_id")
            if not isinstance(turn_id, str) or turn_id == skip_turn_id:
                continue

            kind = item.get("kind")
            if kind not in {"user_message", "agent_message"}:
                continue

            text = self._read_item_text(item)
            if text is None:
                continue

            turn = turns.setdefault(turn_id, {})
            if turn_id not in turn_order:
                turn_order.append(turn_id)
            turn[kind if kind == "user_message" else "assistant"] = text

        for turn_id in turn_order:
            pair = turns.get(turn_id, {})
            user_text = pair.get("user_message")
            assistant_text = pair.get("assistant")
            if user_text is not None and assistant_text is not None:
                pairs.append((user_text, assistant_text))

        if not pairs:
            return None

        selected = pairs[-max_turns:]
        lines: list[str] = []
        for index, (user_text, assistant_text) in enumerate(selected, 1):
            lines.append(f"Turn {index}:")
            lines.append(f"User: {user_text}")
            lines.append(f"Assistant: {assistant_text}")
            lines.append("")
        return "\n".join(lines).strip()

    def _read_item_text(self, item: dict[str, Any]) -> str | None:
        content = item.get("content")
        if not isinstance(content, dict):
            return None
        text = content.get("text")
        if isinstance(text, str):
            return text.strip()
        return None

    def _build_thread_from_payload(self, raw_thread: dict[str, Any]) -> ThreadRecord:
        thread_id = raw_thread.get("id")
        if not isinstance(thread_id, str):
            raise ValueError("thread record missing id")
        cwd = raw_thread.get("cwd")
        if not isinstance(cwd, str):
            raise ValueError("thread record missing cwd")
        title = raw_thread.get("title")
        if title is not None and not isinstance(title, str):
            raise ValueError("thread record title is invalid")
        status_raw = raw_thread.get("status")
        status = ThreadStatus.ACTIVE
        if isinstance(status_raw, str):
            try:
                status = ThreadStatus(status_raw)
            except ValueError:
                status = ThreadStatus.ACTIVE
        created_at_ms = raw_thread.get("created_at_ms")
        if not isinstance(created_at_ms, int):
            created_at_ms = now_ms()
        return ThreadRecord(
            id=thread_id,
            cwd=cwd,
            status=status,
            created_at_ms=created_at_ms,
            title=title,
        )
