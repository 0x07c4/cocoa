from __future__ import annotations

from dataclasses import dataclass, field, is_dataclass
from enum import StrEnum
from pathlib import Path
from time import time
from typing import Any
from uuid import uuid4


def now_ms() -> int:
    return int(time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class ThreadStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class TurnStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ItemKind(StrEnum):
    USER_MESSAGE = "user_message"
    AGENT_MESSAGE = "agent_message"
    PLAN = "plan"
    COMMAND = "command"
    WORKSPACE_INSPECT = "workspace_inspect"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"


class ItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class ApprovalState(StrEnum):
    NONE = "none"
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class EventKind(StrEnum):
    THREAD_STARTED = "thread_started"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    ROUTING_DECISION = "routing_decision"
    USAGE_RECORDED = "usage_recorded"
    ITEM_STARTED = "item_started"
    ITEM_UPDATED = "item_updated"
    ITEM_COMPLETED = "item_completed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    ERROR = "error"


@dataclass(frozen=True)
class ThreadRecord:
    id: str
    cwd: str
    status: ThreadStatus = ThreadStatus.ACTIVE
    created_at_ms: int = field(default_factory=now_ms)
    title: str | None = None


@dataclass(frozen=True)
class TurnRecord:
    id: str
    thread_id: str
    intent: str
    status: TurnStatus = TurnStatus.PENDING
    created_at_ms: int = field(default_factory=now_ms)
    completed_at_ms: int | None = None
    summary: str | None = None


@dataclass(frozen=True)
class ItemRecord:
    id: str
    thread_id: str
    turn_id: str
    kind: ItemKind
    status: ItemStatus
    content: dict[str, Any]
    approval: ApprovalState = ApprovalState.NONE
    created_at_ms: int = field(default_factory=now_ms)
    completed_at_ms: int | None = None


@dataclass(frozen=True)
class RuntimeEvent:
    id: str
    kind: EventKind
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at_ms: int = field(default_factory=now_ms)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: to_jsonable(val) for key, val in value.__dict__.items()}
    if isinstance(value, dict):
        return {str(key): to_jsonable(val) for key, val in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    return value


def event(
    kind: EventKind,
    *,
    thread_id: str | None = None,
    turn_id: str | None = None,
    item_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        id=new_id("evt"),
        kind=kind,
        thread_id=thread_id,
        turn_id=turn_id,
        item_id=item_id,
        payload=payload or {},
    )
