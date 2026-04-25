from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import RuntimeEvent, to_jsonable


@dataclass(frozen=True)
class ThreadLog:
    thread_id: str
    path: Path
    event_count: int


class JsonlStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.threads_dir = root / "threads"
        self.threads_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_workspace(cls, cwd: Path) -> "JsonlStore":
        return cls(cwd / ".cocoa")

    def thread_path(self, thread_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", thread_id):
            raise ValueError(f"invalid thread_id: {thread_id!r}")
        path = (self.threads_dir / f"{thread_id}.jsonl").resolve()
        try:
            path.relative_to(self.threads_dir.resolve())
        except ValueError as exc:
            raise ValueError(f"thread_id escapes store: {thread_id!r}") from exc
        return path

    def append(self, runtime_event: RuntimeEvent) -> None:
        if runtime_event.thread_id is None:
            raise ValueError("runtime event must include thread_id")
        path = self.thread_path(runtime_event.thread_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(to_jsonable(runtime_event), sort_keys=True))
            handle.write("\n")

    def append_many(self, events: Iterable[RuntimeEvent]) -> None:
        for runtime_event in events:
            self.append(runtime_event)

    def read_thread(self, thread_id: str) -> list[dict]:
        path = self.thread_path(thread_id)
        if not path.exists():
            return []
        rows = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def list_threads(self) -> list[ThreadLog]:
        logs: list[ThreadLog] = []
        for path in sorted(self.threads_dir.glob("*.jsonl")):
            event_count = 0
            with path.open("r", encoding="utf-8") as handle:
                for _ in handle:
                    event_count += 1
            logs.append(ThreadLog(path.stem, path, event_count))
        return logs
