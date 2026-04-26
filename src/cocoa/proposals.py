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


def parse_command_proposals(message: str) -> tuple[str, tuple[CommandProposal, ...]]:
    proposals: list[CommandProposal] = []

    def remove_block(match: re.Match[str]) -> str:
        body = match.group("body").strip()
        proposals.extend(_parse_proposal_block(body))
        return ""

    cleaned = _PROPOSAL_BLOCK_RE.sub(remove_block, message).strip()
    return cleaned, tuple(proposals)


def _parse_proposal_block(body: str) -> list[CommandProposal]:
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, dict):
        return []
    commands = raw.get("commands")
    if not isinstance(commands, list):
        return []

    proposals: list[CommandProposal] = []
    for item in commands:
        proposal = _command_proposal_from_raw(item)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


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
