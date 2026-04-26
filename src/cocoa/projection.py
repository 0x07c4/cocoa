from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .store import JsonlStore


@dataclass(frozen=True)
class ItemView:
    id: str
    thread_id: str
    turn_id: str
    kind: str
    status: str
    content: dict[str, Any]
    approval: str
    created_at_ms: int | None = None
    completed_at_ms: int | None = None


@dataclass(frozen=True)
class TurnView:
    id: str
    thread_id: str
    intent: str
    status: str
    items: tuple[ItemView, ...]
    errors: tuple[dict[str, str], ...]
    created_at_ms: int | None = None
    completed_at_ms: int | None = None
    summary: str | None = None


@dataclass(frozen=True)
class ThreadView:
    id: str
    cwd: str
    status: str
    turns: tuple[TurnView, ...]
    event_count: int
    created_at_ms: int | None = None
    title: str | None = None

    def find_turn(self, turn_id: str) -> TurnView | None:
        for turn in self.turns:
            if turn.id == turn_id:
                return turn
        return None

    def find_item(self, item_id: str) -> ItemView | None:
        for turn in self.turns:
            for item in turn.items:
                if item.id == item_id:
                    return item
        return None

    @property
    def last_turn(self) -> TurnView | None:
        if not self.turns:
            return None
        return self.turns[-1]


def load_thread_view(store: JsonlStore, thread_id: str) -> ThreadView:
    rows = store.read_thread(thread_id)
    if not rows:
        raise ValueError(f"thread not found: {thread_id}")
    return project_thread(rows)


def project_thread(rows: Iterable[Mapping[str, Any]]) -> ThreadView:
    events = list(rows)
    thread_data: dict[str, Any] | None = None
    turn_data: dict[str, dict[str, Any]] = {}
    turn_order: list[str] = []
    item_data: dict[str, dict[str, Any]] = {}
    item_order_by_turn: dict[str, list[str]] = {}
    errors_by_turn: dict[str, list[dict[str, str]]] = {}

    for row in events:
        kind = _string(row.get("kind"))
        payload = _mapping(row.get("payload"))

        if kind == "thread_started":
            raw_thread = _mapping(payload.get("thread"))
            if raw_thread:
                thread_data = dict(raw_thread)
            continue

        if kind in {"turn_started", "turn_completed"}:
            raw_turn = _mapping(payload.get("turn"))
            if not raw_turn:
                continue
            turn_id = _string(raw_turn.get("id"))
            if turn_id is None:
                continue
            if turn_id not in turn_order:
                turn_order.append(turn_id)
            current = turn_data.get(turn_id, {})
            current.update(raw_turn)
            turn_data[turn_id] = current
            continue

        if kind in {
            "approval_requested",
            "item_started",
            "item_updated",
            "item_completed",
        }:
            raw_item = _mapping(payload.get("item"))
            if not raw_item:
                continue
            item_id = _string(raw_item.get("id"))
            turn_id = _string(raw_item.get("turn_id"))
            if item_id is None or turn_id is None:
                continue
            current = item_data.get(item_id, {})
            current.update(raw_item)
            item_data[item_id] = current
            item_order = item_order_by_turn.setdefault(turn_id, [])
            if item_id not in item_order:
                item_order.append(item_id)
            continue

        if kind == "approval_resolved":
            item_id = _string(row.get("item_id"))
            if item_id is None:
                continue
            current = item_data.setdefault(item_id, {})
            approved = payload.get("approved")
            if approved is True:
                current["approval"] = "accepted"
            elif approved is False:
                current["approval"] = "rejected"
            continue

        if kind == "error":
            turn_id = _string(row.get("turn_id"))
            if turn_id is None:
                continue
            message = _string(payload.get("message")) or ""
            error_type = _string(payload.get("type")) or "Error"
            errors_by_turn.setdefault(turn_id, []).append(
                {"type": error_type, "message": message}
            )

    if thread_data is None:
        raise ValueError("thread started event missing")

    thread_id = _required_string(thread_data, "id")
    turns: list[TurnView] = []
    for turn_id in turn_order:
        raw_turn = turn_data.get(turn_id, {})
        items = tuple(
            _item_view(item_data[item_id])
            for item_id in item_order_by_turn.get(turn_id, [])
            if item_id in item_data
        )
        turns.append(
            TurnView(
                id=turn_id,
                thread_id=_string(raw_turn.get("thread_id")) or thread_id,
                intent=_string(raw_turn.get("intent")) or "unknown",
                status=_string(raw_turn.get("status")) or "pending",
                created_at_ms=_int_or_none(raw_turn.get("created_at_ms")),
                completed_at_ms=_int_or_none(raw_turn.get("completed_at_ms")),
                summary=_string(raw_turn.get("summary")),
                items=items,
                errors=tuple(errors_by_turn.get(turn_id, [])),
            )
        )

    return ThreadView(
        id=thread_id,
        cwd=_string(thread_data.get("cwd")) or "",
        status=_string(thread_data.get("status")) or "active",
        created_at_ms=_int_or_none(thread_data.get("created_at_ms")),
        title=_string(thread_data.get("title")),
        turns=tuple(turns),
        event_count=len(events),
    )


def _item_view(raw_item: Mapping[str, Any]) -> ItemView:
    content = raw_item.get("content")
    if not isinstance(content, dict):
        content = {}
    return ItemView(
        id=_required_string(raw_item, "id"),
        thread_id=_string(raw_item.get("thread_id")) or "",
        turn_id=_string(raw_item.get("turn_id")) or "",
        kind=_string(raw_item.get("kind")) or "unknown",
        status=_string(raw_item.get("status")) or "pending",
        content=dict(content),
        approval=_string(raw_item.get("approval")) or "none",
        created_at_ms=_int_or_none(raw_item.get("created_at_ms")),
        completed_at_ms=_int_or_none(raw_item.get("completed_at_ms")),
    )


def _required_string(source: Mapping[str, Any], key: str) -> str:
    value = _string(source.get(key))
    if value is None:
        raise ValueError(f"record missing {key}")
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _string(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    return None
