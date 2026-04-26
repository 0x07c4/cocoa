from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
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
from .proposals import CommandProposal, parse_command_proposals
from .store import JsonlStore
from .tools import CommandResult, ShellTool


@dataclass(frozen=True)
class UserTurnResult:
    message: str
    proposals: tuple[ItemRecord, ...] = ()


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
        try:
            response = await self.provider.complete(
                ProviderRequest(
                    thread_id=thread.id,
                    turn_id=turn.id,
                    prompt=prompt,
                    cwd=thread.cwd,
                    thread_context=context,
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

        message, command_proposals = parse_command_proposals(response.message)
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
        proposal_items = tuple(
            self._command_proposal_item(thread, turn, proposal)
            for proposal in command_proposals
        )
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
        return UserTurnResult(message=message, proposals=proposal_items)

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

    def _find_pending_command(self, thread: ThreadRecord, item_id: str) -> ItemRecord:
        item: dict[str, Any] | None = None
        for row in self.store.read_thread(thread.id):
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            raw_item = payload.get("item")
            if not isinstance(raw_item, dict) or raw_item.get("id") != item_id:
                continue
            item = raw_item
        if item is None:
            raise ValueError(f"command proposal not found: {item_id}")
        if item.get("kind") != ItemKind.COMMAND.value:
            raise ValueError(f"item is not a command proposal: {item_id}")
        if item.get("status") != ItemStatus.PENDING.value:
            raise ValueError(f"command proposal is not pending: {item_id}")
        if item.get("approval") != ApprovalState.REQUESTED.value:
            raise ValueError(f"command proposal is not awaiting approval: {item_id}")
        return ItemRecord(
            id=item_id,
            thread_id=thread.id,
            turn_id=str(item.get("turn_id")),
            kind=ItemKind.COMMAND,
            status=ItemStatus.PENDING,
            content=dict(item.get("content") if isinstance(item.get("content"), dict) else {}),
            approval=ApprovalState.REQUESTED,
            created_at_ms=(
                item.get("created_at_ms")
                if isinstance(item.get("created_at_ms"), int)
                else now_ms()
            ),
        )

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
