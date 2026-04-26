from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


_PROPOSAL_BLOCK_RE = re.compile(
    r"```cocoa-proposal\s*(?P<body>.*?)```",
    re.DOTALL,
)


@dataclass(frozen=True)
class CommandProposal:
    command: str
    reason: str | None = None


@dataclass(frozen=True)
class FileWriteProposal:
    path: str
    content: str
    reason: str | None = None


@dataclass(frozen=True)
class FileEditProposal:
    path: str
    old: str
    new: str
    reason: str | None = None
    replace_all: bool = False


@dataclass(frozen=True)
class ParsedProposals:
    message: str
    commands: tuple[CommandProposal, ...] = ()
    file_writes: tuple[FileWriteProposal, ...] = ()
    file_edits: tuple[FileEditProposal, ...] = ()


def parse_proposals(message: str) -> ParsedProposals:
    commands: list[CommandProposal] = []
    file_writes: list[FileWriteProposal] = []
    file_edits: list[FileEditProposal] = []

    def remove_block(match: re.Match[str]) -> str:
        body = match.group("body").strip()
        parsed_commands, parsed_file_writes, parsed_file_edits = _parse_proposal_block(body)
        commands.extend(parsed_commands)
        file_writes.extend(parsed_file_writes)
        file_edits.extend(parsed_file_edits)
        return ""

    cleaned = _PROPOSAL_BLOCK_RE.sub(remove_block, message).strip()
    return ParsedProposals(
        message=cleaned,
        commands=tuple(commands),
        file_writes=tuple(file_writes),
        file_edits=tuple(file_edits),
    )


def parse_command_proposals(message: str) -> tuple[str, tuple[CommandProposal, ...]]:
    parsed = parse_proposals(message)
    return parsed.message, parsed.commands


def _parse_proposal_block(
    body: str,
) -> tuple[list[CommandProposal], list[FileWriteProposal], list[FileEditProposal]]:
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return [], [], []
    if not isinstance(raw, dict):
        return [], [], []

    commands = _parse_command_proposals(raw.get("commands"))
    file_writes = _parse_file_write_proposals(raw.get("write_files"))
    file_edits = _parse_file_edit_proposals(raw.get("edits"))
    file_edits.extend(_parse_file_edit_proposals(raw.get("file_edits")))
    return commands, file_writes, file_edits


def _parse_command_proposals(raw_commands: Any) -> list[CommandProposal]:
    if not isinstance(raw_commands, list):
        return []
    proposals: list[CommandProposal] = []
    for item in raw_commands:
        proposal = _command_proposal_from_raw(item)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def _parse_file_write_proposals(raw_writes: Any) -> list[FileWriteProposal]:
    if not isinstance(raw_writes, list):
        return []
    proposals: list[FileWriteProposal] = []
    for item in raw_writes:
        proposal = _file_write_proposal_from_raw(item)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def _parse_file_edit_proposals(raw_edits: Any) -> list[FileEditProposal]:
    if not isinstance(raw_edits, list):
        return []
    proposals: list[FileEditProposal] = []
    for item in raw_edits:
        proposal = _file_edit_proposal_from_raw(item)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def _file_edit_proposal_from_raw(raw: Any) -> FileEditProposal | None:
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    old = raw.get("old")
    new = raw.get("new")
    if not isinstance(path, str) or not isinstance(old, str) or not isinstance(new, str):
        return None
    path = path.strip()
    if not path or not old:
        return None
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = None
    else:
        reason = reason.strip()
    replace_all = raw.get("replace_all")
    return FileEditProposal(
        path=path,
        old=old,
        new=new,
        reason=reason,
        replace_all=replace_all is True,
    )


def _file_write_proposal_from_raw(raw: Any) -> FileWriteProposal | None:
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    content = raw.get("content")
    if not isinstance(path, str) or not isinstance(content, str):
        return None
    path = path.strip()
    if not path:
        return None
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = None
    else:
        reason = reason.strip()
    return FileWriteProposal(path=path, content=content, reason=reason)


def _command_proposal_from_raw(raw: Any) -> CommandProposal | None:
    if not isinstance(raw, dict):
        return None
    command = raw.get("command")
    if not isinstance(command, str):
        return None
    command = command.strip()
    if not command:
        return None
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = None
    else:
        reason = reason.strip()
    return CommandProposal(command=command, reason=reason)
