from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    ItemKind,
    ItemRecord,
    ItemStatus,
    new_id,
    now_ms,
)
from .workspace import WorkspaceScope

_PATH_REFERENCE_RE = re.compile(
    r"(?<![\w@])@(?P<path>[A-Za-z0-9][A-Za-z0-9._/\-]*)(?=$|[\s,.;:!?)}\]])"
)

_MAX_GIT_STATUS_LINES = 40
_MAX_GIT_STATUS_CHARS = 4_000
_MAX_WORKSPACE_MAP_ENTRIES = 80
_MAX_CONTEXT_REFERENCES = 6
_MAX_CONTEXT_FILE_BYTES = 32_000
_MAX_WORKSPACE_CONTEXT_CHARS = 90_000


@dataclass(frozen=True)
class BuiltContext:
    text: str | None
    items: tuple[ItemRecord, ...]


def build_workspace_context(
    scope: WorkspaceScope,
    thread_id: str,
    turn_id: str,
    prompt: str,
) -> BuiltContext:
    sections: list[str] = []
    items: list[ItemRecord] = []

    date_section = _build_date_context()
    sections.append(date_section)

    git_section = _build_git_status_context(scope.root)
    if git_section:
        sections.append(git_section)

    instr_sections, instr_items = _build_instruction_file_context(
        scope, thread_id, turn_id,
    )
    sections.extend(instr_sections)
    items.extend(instr_items)

    map_section = _build_workspace_map(scope)
    if map_section:
        sections.append(map_section)

    ref_section, ref_items = _build_path_references(
        scope, thread_id, turn_id, prompt,
    )
    if ref_section:
        sections.append(ref_section)
    items.extend(ref_items)

    text = "\n\n".join(section for section in sections if section.strip()).strip()
    if not text:
        return BuiltContext(text=None, items=tuple(items))
    if len(text) > _MAX_WORKSPACE_CONTEXT_CHARS:
        text = (
            text[:_MAX_WORKSPACE_CONTEXT_CHARS]
            + "\n\n[workspace context truncated by cocoa]"
        )
    return BuiltContext(text=text, items=tuple(items))


def _build_date_context() -> str:
    now = datetime.now()
    return f"Current date and time: {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"


def _build_git_status_context(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    if not output:
        return "Git status: clean"

    lines = output.splitlines()
    capped = False
    if len(lines) > _MAX_GIT_STATUS_LINES:
        lines = lines[:_MAX_GIT_STATUS_LINES]
        capped = True

    text = "\n".join(lines)
    if len(text) > _MAX_GIT_STATUS_CHARS:
        text = text[:_MAX_GIT_STATUS_CHARS]
        capped = True

    result_text = f"Git status:\n{text}"
    if capped:
        result_text += "\n[git status truncated by cocoa]"
    return result_text


def _build_instruction_file_context(
    scope: WorkspaceScope,
    thread_id: str,
    turn_id: str,
) -> tuple[list[str], list[ItemRecord]]:
    sections: list[str] = []
    items: list[ItemRecord] = []
    candidates = ["AGENTS.md", "CLAUDE.md"]

    for filename in candidates:
        try:
            target = scope.resolve(filename)
        except ValueError:
            continue
        if not target.is_file():
            continue
        relative = target.relative_to(scope.root).as_posix()
        if scope.is_ignored(relative):
            continue
        try:
            with target.open("rb") as handle:
                raw = handle.read(_MAX_CONTEXT_FILE_BYTES + 1)
            if b"\x00" in raw:
                continue
            truncated = len(raw) > _MAX_CONTEXT_FILE_BYTES
            text = raw[:_MAX_CONTEXT_FILE_BYTES].decode("utf-8", errors="replace")
        except (OSError, UnicodeError):
            continue

        item = ItemRecord(
            id=new_id("item"),
            thread_id=thread_id,
            turn_id=turn_id,
            kind=ItemKind.FILE_READ,
            status=ItemStatus.COMPLETED,
            content={
                "path": relative,
                "size": target.stat().st_size,
                "truncated": truncated,
                "source": "instruction_file",
                "text": text,
            },
            completed_at_ms=now_ms(),
        )
        items.append(item)
        header = f"Instruction file: @{relative}"
        if truncated:
            header += f" (first {_MAX_CONTEXT_FILE_BYTES} bytes)"
        sections.append(f"{header}\n<file path=\"{relative}\">\n{text}\n</file>")

    return sections, items


def _build_workspace_map(scope: WorkspaceScope) -> str | None:
    try:
        entries = scope.inspect(".", max_entries=_MAX_WORKSPACE_MAP_ENTRIES + 1)
    except (OSError, ValueError) as exc:
        return f"Workspace file map unavailable: {exc}"
    if not entries:
        return "Workspace file map: empty workspace"
    reached_cap = len(entries) > _MAX_WORKSPACE_MAP_ENTRIES
    if reached_cap:
        entries = entries[:_MAX_WORKSPACE_MAP_ENTRIES]
    lines = [
        f"Workspace file map (first {len(entries)} visible entries; ignored paths omitted):"
    ]
    for entry in entries:
        lines.append(f"- {entry.path} ({entry.size} bytes)")
    if reached_cap:
        lines.append(f"[workspace map capped at {_MAX_WORKSPACE_MAP_ENTRIES} entries by cocoa]")
    return "\n".join(lines)


def extract_path_references(prompt: str) -> tuple[str, ...]:
    seen: set[str] = set()
    references: list[str] = []
    for match in _PATH_REFERENCE_RE.finditer(prompt):
        path = match.group("path").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        references.append(path)
    return tuple(references)


def _build_path_references(
    scope: WorkspaceScope,
    thread_id: str,
    turn_id: str,
    prompt: str,
) -> tuple[str | None, list[ItemRecord]]:
    references = extract_path_references(prompt)
    if not references:
        return None, []

    sections: list[str] = ["Referenced workspace paths:"]
    items: list[ItemRecord] = []

    for raw_path in references[:_MAX_CONTEXT_REFERENCES]:
        item, context_text = _referenced_path_context_item(
            scope, thread_id, turn_id, raw_path,
        )
        items.append(item)
        sections.append(context_text)

    skipped = len(references) - _MAX_CONTEXT_REFERENCES
    if skipped > 0:
        sections.append(f"- skipped {skipped} additional @path reference(s)")

    return "\n\n".join(sections), items


def _referenced_path_context_item(
    scope: WorkspaceScope,
    thread_id: str,
    turn_id: str,
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
            return _referenced_directory_context_item(
                scope, thread_id, turn_id, relative,
            )
        if target.is_file():
            return _referenced_file_context_item(
                thread_id, turn_id, target, relative,
            )
        raise ValueError(f"path is not a regular file or directory: {relative}")
    except (OSError, UnicodeError, ValueError) as exc:
        item = ItemRecord(
            id=new_id("item"),
            thread_id=thread_id,
            turn_id=turn_id,
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
    thread_id: str,
    turn_id: str,
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
        thread_id=thread_id,
        turn_id=turn_id,
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
    scope: WorkspaceScope,
    thread_id: str,
    turn_id: str,
    relative: str,
) -> tuple[ItemRecord, str]:
    entries = scope.inspect(relative, max_entries=_MAX_WORKSPACE_MAP_ENTRIES + 1)
    reached_cap = len(entries) > _MAX_WORKSPACE_MAP_ENTRIES
    if reached_cap:
        entries = entries[:_MAX_WORKSPACE_MAP_ENTRIES]
    payload_entries = [{"path": entry.path, "size": entry.size} for entry in entries]
    item = ItemRecord(
        id=new_id("item"),
        thread_id=thread_id,
        turn_id=turn_id,
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
    if reached_cap:
        lines.append(f"- ... capped at {_MAX_WORKSPACE_MAP_ENTRIES} entries")
    return item, "\n".join(lines)
