from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from . import config as cocoa_config
from .models import ItemKind, ItemRecord
from .projection import ItemView, TurnView, load_thread_view
from .providers import (
    ProviderConfigurationError,
    StubProvider,
    provider_from_env,
    provider_name_from_env,
)
from .runtime import AgentRuntime
from .store import JsonlStore
from .tools import ConsoleApprovalPrompter, ShellTool
from .workspace import WorkspaceScope

_REPL_COMMANDS = (
    "/accept",
    "/apply",
    "/configure",
    "/diff",
    "/escalate",
    "/exit",
    "/help",
    "/history",
    "/inspect",
    "/model",
    "/mode",
    "/pending",
    "/persist",
    "/provider",
    "/quit",
    "/reject",
    "/run",
    "/set",
    "/show",
    "/status",
    "/task",
    "/task-add",
    "/task-update",
    "/tasks",
    "/usage",
)

_CONFIGURE_MODES = ("clear", "codex-http", "openai")
_MODEL_MODES = ("balanced", "cheap", "premium", "local")
_MODEL_MODE_ENV = "COCOA_MODEL_MODE"
_SET_OPTIONS = ("--persist", "-p")

_REPL_COMMAND_DESCRIPTIONS: Mapping[str, str] = {
    "/accept": "run pending command",
    "/apply": "apply pending file write",
    "/configure": "configure provider",
    "/diff": "show pending file diff",
    "/escalate": "escalate last turn to reviewer",
    "/exit": "quit cocoa",
    "/help": "show commands",
    "/history": "show thread turns",
    "/inspect": "list workspace files",
    "/model": "show model",
    "/mode": "show or set routing mode",
    "/pending": "show pending proposals",
    "/persist": "save session config",
    "/provider": "show provider",
    "/quit": "quit cocoa",
    "/reject": "reject pending proposal",
    "/run": "run shell command",
    "/set": "set session variable",
    "/show": "show turn or item",
    "/status": "show session status",
    "/task": "show task details",
    "/task-add": "create a new task",
    "/task-update": "update a task",
    "/tasks": "list current tasks",
    "/usage": "show model usage",
}

_CONFIGURE_MODE_DESCRIPTIONS: Mapping[str, str] = {
    "clear": "clear workspace provider override",
    "codex-http": "use local Codex auth over HTTP",
    "openai": "use OpenAI-compatible API",
}

_SET_OPTION_DESCRIPTIONS: Mapping[str, str] = {
    "--persist": "also write to .cocoa/cocoa.toml",
    "-p": "also write to .cocoa/cocoa.toml",
}

_COMMANDS_EXPECTING_ARGUMENTS = {
    "/accept",
    "/apply",
    "/configure",
    "/diff",
    "/escalate",
    "/inspect",
    "/mode",
    "/reject",
    "/run",
    "/set",
    "/show",
    "/task",
    "/task-add",
    "/task-update",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_KNOWN_ESCAPE_KEYS = {
    "\x1b[A",
    "\x1b[B",
    "\x1b[C",
    "\x1b[D",
    "\x1b[H",
    "\x1b[F",
    "\x1b[1~",
    "\x1b[3~",
    "\x1b[4~",
}


class ReplInput:
    def __init__(self, cwd: Path, store: JsonlStore, thread_id: str) -> None:
        self.cwd = cwd
        self.store = store
        self.thread_id = thread_id
        self.color = _should_use_color()
        self._session = (
            _create_prompt_toolkit_session(cwd, store, thread_id)
            if sys.stdin.isatty()
            else None
        )
        self._native_composer = (
            _create_native_composer(cwd, store, thread_id)
            if sys.stdin.isatty() and self._session is None
            else None
        )
        self._restore_readline = (
            _install_repl_completion(cwd, store, thread_id)
            if sys.stdin.isatty()
            and self._session is None
            and self._native_composer is None
            else (lambda: None)
        )

    def read(self, provider_status: str) -> str:
        if self._session is not None:
            response = self._session.prompt(
                _prompt_toolkit_fragments(provider_status, self.thread_id),
                bottom_toolbar=_prompt_toolkit_toolbar(provider_status),
                wrap_lines=True,
            )
            return str(response)
        if self._native_composer is not None:
            return self._native_composer.read(provider_status)
        return input(_format_repl_prompt(provider_status, self.thread_id, color=self.color))

    def close(self) -> None:
        self._restore_readline()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cocoa",
        description="Terminal-native agentic coding system.",
    )
    parser.add_argument("--version", action="version", version=f"cocoa {__version__}")
    parser.set_defaults(command="repl", cwd=".", thread=None)
    subparsers = parser.add_subparsers(dest="command", required=False)

    doctor = subparsers.add_parser("doctor", help="Print local runtime status.")
    doctor.add_argument("--cwd", default=".", help="Workspace directory.")

    inspect = subparsers.add_parser("inspect", help="Inspect workspace files.")
    inspect.add_argument("path", nargs="?", default=".", help="Path inside workspace.")
    inspect.add_argument("--cwd", default=".", help="Workspace directory.")
    inspect.add_argument("--max", type=positive_int, default=120, help="Maximum entries.")

    ask = subparsers.add_parser("ask", help="Run one recorded user turn.")
    ask.add_argument("prompt", help="User prompt.")
    ask.add_argument("--cwd", default=".", help="Workspace directory.")
    ask.add_argument("--thread", help="Resume an existing thread id.")

    repl = subparsers.add_parser("repl", help="Start a line-oriented cocoa session.")
    repl.add_argument("--cwd", default=".", help="Workspace directory.")
    repl.add_argument("--thread", help="Resume an existing thread id.")

    threads = subparsers.add_parser("threads", help="List recorded threads.")
    threads.add_argument("--cwd", default=".", help="Workspace directory.")

    return parser


def positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def _parse_config_lines(raw: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if not value:
            env[key] = ""
            continue
        try:
            tokens = shlex.split(value)
        except ValueError:
            env[key] = value.strip("'\"")
        else:
            if len(tokens) == 0:
                env[key] = ""
            elif len(tokens) == 1:
                env[key] = tokens[0]
            else:
                env[key] = value
    return env


def _effective_environment(cwd: Path) -> dict[str, str]:
    config = cocoa_config.resolve_config(cwd, {})
    mode = cocoa_config.resolve_model_mode(config)
    return cocoa_config.to_env_mapping(config, mode)


def _persist_environment(cwd: Path, updates: Mapping[str, str]) -> None:
    data = cocoa_config.load_workspace_config(cwd)
    data = cocoa_config.merge_session_overrides(data, updates)
    cocoa_config.save_workspace_config(cwd, data)


def _effective_session_environment(
    cwd: Path,
    overrides: Mapping[str, str],
) -> dict[str, str]:
    config = cocoa_config.resolve_config(cwd, overrides)
    mode = cocoa_config.resolve_model_mode(config)
    return cocoa_config.to_env_mapping(config, mode)


def _resolve_model_mode(env: Mapping[str, str]) -> str:
    raw = env.get(_MODEL_MODE_ENV, "").strip().lower()
    if raw in _MODEL_MODES:
        return raw
    return "balanced"


def _provider_environment_for_mode(env: Mapping[str, str]) -> tuple[dict[str, str], bool]:
    mode = _resolve_model_mode(env)
    prefix = f"COCOA_{mode.upper()}_"
    routed = dict(env)
    profile_used = False

    provider = env.get(f"{prefix}PROVIDER")
    if provider:
        routed["COCOA_PROVIDER"] = provider
        profile_used = True
    elif any(key.startswith(f"{prefix}OPENAI_") for key in env):
        routed["COCOA_PROVIDER"] = "openai"
        profile_used = True
    elif any(key.startswith(f"{prefix}CODEX_") for key in env):
        routed["COCOA_PROVIDER"] = "codex-http"
        profile_used = True

    key_map = {
        "OPENAI_API_KEY": "COCOA_OPENAI_API_KEY",
        "OPENAI_MODEL": "COCOA_OPENAI_MODEL",
        "OPENAI_BASE_URL": "COCOA_OPENAI_BASE_URL",
        "OPENAI_TIMEOUT_SECONDS": "COCOA_OPENAI_TIMEOUT_SECONDS",
        "OPENAI_TEMPERATURE": "COCOA_OPENAI_TEMPERATURE",
        "OPENAI_MAX_TOKENS": "COCOA_OPENAI_MAX_TOKENS",
        "CODEX_API_KEY": "COCOA_CODEX_API_KEY",
        "CODEX_MODEL": "COCOA_CODEX_MODEL",
        "CODEX_BASE_URL": "COCOA_CODEX_BASE_URL",
        "CODEX_HOME": "COCOA_CODEX_HOME",
        "CODEX_TIMEOUT_SECONDS": "COCOA_CODEX_TIMEOUT_SECONDS",
        "CODEX_TEMPERATURE": "COCOA_CODEX_TEMPERATURE",
        "CODEX_MAX_TOKENS": "COCOA_CODEX_MAX_TOKENS",
    }
    for source_suffix, target_key in key_map.items():
        source_key = f"{prefix}{source_suffix}"
        if source_key in env:
            routed[target_key] = env[source_key]
            profile_used = True

    generic_model = env.get(f"{prefix}MODEL")
    if generic_model:
        provider_name = routed.get("COCOA_PROVIDER", "").lower()
        if provider_name in {"codex", "codex-http", "codex-responses", "openai-codex"}:
            routed["COCOA_CODEX_MODEL"] = generic_model
        else:
            routed["COCOA_OPENAI_MODEL"] = generic_model
        profile_used = True

    return routed, profile_used


def _resolve_runtime_from_env(cwd: Path, overrides: Mapping[str, str]) -> tuple[
    AgentRuntime,
    JsonlStore,
    str,
]:
    env = _effective_session_environment(cwd, overrides)
    provider_env, _ = _provider_environment_for_mode(env)
    store = JsonlStore.for_workspace(cwd)
    config = cocoa_config.resolve_config(cwd, overrides)
    budget = config.get("budget", {}) if isinstance(config.get("budget"), dict) else {}
    try:
        provider_status = _resolve_provider_status_for_env(provider_env)
        provider = provider_from_env(provider_env)
    except ProviderConfigurationError as exc:
        provider_status = f"not configured ({exc})"
        provider = StubProvider()
    return AgentRuntime(store=store, provider=provider, budget=budget), store, provider_status


def resolve_cwd(raw: str) -> Path:
    cwd = Path(raw).expanduser().resolve()
    if not cwd.exists():
        raise SystemExit(f"workspace does not exist: {cwd}")
    if not cwd.is_dir():
        raise SystemExit(f"workspace is not a directory: {cwd}")
    return cwd


def make_runtime(cwd: Path) -> tuple[AgentRuntime, JsonlStore]:
    store = JsonlStore.for_workspace(cwd)
    env = _effective_environment(cwd)
    provider_env, _ = _provider_environment_for_mode(env)
    config = cocoa_config.resolve_config(cwd, {})
    budget = config.get("budget", {}) if isinstance(config.get("budget"), dict) else {}
    return AgentRuntime(store=store, provider=provider_from_env(provider_env), budget=budget), store


def print_doctor(cwd: Path) -> None:
    try:
        env = _effective_environment(cwd)
        provider_env, profile_used = _provider_environment_for_mode(env)
        provider_name = provider_name_from_env(provider_env)
    except ProviderConfigurationError as exc:
        env = _effective_environment(cwd)
        profile_used = False
        provider_name = f"not configured ({exc})"
    git_root = None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            git_root = completed.stdout.strip()
    except OSError:
        git_root = None

    print(f"cocoa: {__version__}")
    print(f"python: {sys.version.split()[0]}")
    print(f"cwd: {cwd}")
    print(f"state: {cwd / '.cocoa'}")
    print(f"git_root: {git_root or '(none)'}")
    print(f"mode: {_resolve_model_mode(env)}{' (profile)' if profile_used else ''}")
    print(f"provider: {provider_name}")


def print_inspect(cwd: Path, path: str, max_entries: int) -> None:
    scope = WorkspaceScope(cwd)
    entries = scope.inspect(path, max_entries=max_entries)
    for entry in entries:
        print(f"{entry.path}\t{entry.size}")
    if len(entries) >= max_entries:
        print(f"... capped at {max_entries} entries")


def print_history(store: JsonlStore, thread_id: str) -> None:
    view = load_thread_view(store, thread_id)
    print(f"thread: {view.id}")
    if view.title:
        print(f"title: {view.title}")
    if not view.turns:
        print("no turns")
        return
    for turn in view.turns:
        summary = turn.summary or _turn_preview(turn)
        if summary:
            summary = textwrap.shorten(summary.replace("\n", " "), width=80)
        else:
            summary = "-"
        print(
            f"{turn.id}\t{turn.status}\t{turn.intent}\t"
            f"{len(turn.items)} items\t{summary}"
        )


def _print_usage(store: JsonlStore, thread_id: str) -> None:
    rows = store.read_thread(thread_id)
    if not rows:
        print(f"thread not found: {thread_id}")
        return
    records = _usage_records(rows)
    if not records:
        print("no usage recorded")
        return
    total_input = 0
    total_output = 0
    print("usage:")
    for record in records:
        input_tokens = record.get("input_tokens")
        output_tokens = record.get("output_tokens")
        if isinstance(input_tokens, int):
            total_input += input_tokens
        if isinstance(output_tokens, int):
            total_output += output_tokens
        turn_id = str(record.get("turn_id") or "-")
        mode = str(record.get("mode") or "balanced")
        provider = str(record.get("provider") or "unknown")
        model = record.get("model")
        model_text = f":{model}" if isinstance(model, str) and model else ""
        estimated = " ~" if record.get("estimated") is True else "  "
        print(
            f"  {turn_id}\t{mode}\t{provider}{model_text}\t"
            f"{estimated}in={input_tokens or 0} out={output_tokens or 0}"
        )
        warning = record.get("budget_warning")
        if isinstance(warning, str) and warning:
            print(f"    warning: {warning}")
    print(f"total\tin={total_input} out={total_output}")


def _usage_records(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    routing_by_turn: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for row in rows:
        kind = row.get("kind")
        turn_id = row.get("turn_id")
        payload = row.get("payload")
        if not isinstance(turn_id, str) or not isinstance(payload, dict):
            continue
        if kind == "routing_decision":
            routing_by_turn[turn_id] = dict(payload)
            continue
        if kind != "usage_recorded":
            continue
        record = dict(routing_by_turn.get(turn_id, {}))
        record.update(payload)
        record["turn_id"] = turn_id
        records.append(record)
    return records


def print_show(store: JsonlStore, thread_id: str, target: str) -> None:
    view = load_thread_view(store, thread_id)
    if target in {"last", "."}:
        turn = view.last_turn
        if turn is None:
            print("no turns")
            return
        _print_turn_view(turn)
        return

    turn = view.find_turn(target)
    if turn is not None:
        _print_turn_view(turn)
        return

    item = view.find_item(target)
    if item is not None:
        _print_item_view(item)
        return

    print(f"not found: {target}")


def _completion_candidates(
    line: str,
    text: str,
    *,
    cwd: Path,
    store: JsonlStore,
    thread_id: str,
) -> list[str]:
    if not line.startswith("/"):
        if text.startswith("@"):
            return _workspace_reference_completion_candidates(cwd, text)
        return []

    command, has_space, _ = line.partition(" ")
    if not has_space:
        return [command for command in _REPL_COMMANDS if command.startswith(text)]

    if command == "/configure":
        return [mode for mode in _CONFIGURE_MODES if mode.startswith(text)]
    if command == "/mode":
        return [mode for mode in _MODEL_MODES if mode.startswith(text)]
    if command == "/set":
        return [option for option in _SET_OPTIONS if option.startswith(text)]
    if command == "/show":
        return [
            candidate
            for candidate in _show_completion_candidates(store, thread_id)
            if candidate.startswith(text)
        ]
    if command == "/accept":
        return [
            candidate
            for candidate in _pending_command_completion_candidates(store, thread_id)
            if candidate.startswith(text)
        ]
    if command == "/apply":
        return [
            candidate
            for candidate in _pending_file_write_completion_candidates(store, thread_id)
            if candidate.startswith(text)
        ]
    if command == "/diff":
        return [
            candidate
            for candidate in _pending_file_write_completion_candidates(store, thread_id)
            if candidate.startswith(text)
        ]
    if command == "/escalate":
        return [candidate for candidate in ["last"] if candidate.startswith(text)]
    if command == "/reject":
        return [
            candidate
            for candidate in _pending_proposal_completion_candidates(store, thread_id)
            if candidate.startswith(text)
        ]
    if command == "/inspect":
        return _workspace_path_completion_candidates(cwd, text)
    if command == "/run":
        return _run_completion_candidates(cwd, line, text)
    if command == "/task":
        return [
            candidate
            for candidate in _task_item_completion_candidates(store, thread_id)
            if candidate.startswith(text)
        ]
    if command == "/task-update":
        parts = line.split()
        if len(parts) <= 2:
            return [
                candidate
                for candidate in _task_item_completion_candidates(store, thread_id)
                if candidate.startswith(text)
            ]
        if len(parts) == 3 and not line.endswith(" "):
            return []
        if "--status" in parts:
            status_values = ["pending", "in_progress", "completed"]
            return [s for s in status_values if s.startswith(text)]
        return ["--status"]
    return []


def _completion_text(line: str) -> str:
    if line.endswith(" "):
        return ""
    return line.rsplit(" ", 1)[-1]


def _apply_completion_candidate(line: str, candidate: str) -> str:
    if line.endswith(" "):
        completed = f"{line}{candidate}"
    else:
        prefix, separator, _ = line.rpartition(" ")
        completed = f"{prefix}{separator}{candidate}" if separator else candidate
    if completed in _COMMANDS_EXPECTING_ARGUMENTS:
        return f"{completed} "
    return completed


def _apply_completion_candidate_at_cursor(
    line: str,
    cursor: int,
    candidate: str,
) -> tuple[str, int]:
    before_cursor = line[:cursor]
    token_start = before_cursor.rfind(" ") + 1
    next_space_index = line.find(" ", cursor)
    token_end = len(line) if next_space_index < 0 else next_space_index
    completed_before_cursor = line[:token_start] + candidate
    if completed_before_cursor in _COMMANDS_EXPECTING_ARGUMENTS:
        completed_before_cursor = f"{completed_before_cursor} "
    after_token = line[token_end:]
    if completed_before_cursor.endswith(" ") and after_token.startswith(" "):
        after_token = after_token[1:]
    return completed_before_cursor + after_token, len(completed_before_cursor)


def _suggestion_items(
    line: str,
    *,
    cwd: Path,
    store: JsonlStore,
    thread_id: str,
    max_items: int = 24,
) -> list[tuple[str, str]]:
    text = _completion_text(line)
    if not line.startswith("/") and not text.startswith("@"):
        return []
    candidates = _completion_candidates(
        line,
        text,
        cwd=cwd,
        store=store,
        thread_id=thread_id,
    )
    return [
        (candidate, _completion_description(line, candidate))
        for candidate in candidates[:max_items]
    ]


def _completion_description(line: str, candidate: str) -> str:
    if not line.startswith("/") and candidate.startswith("@"):
        return "workspace context"
    command, has_space, _ = line.partition(" ")
    if not has_space:
        return _REPL_COMMAND_DESCRIPTIONS.get(candidate, "")
    if command == "/configure":
        return _CONFIGURE_MODE_DESCRIPTIONS.get(candidate, "")
    if command == "/mode":
        if candidate == "cheap":
            return "prefer cheap API or local models"
        if candidate == "balanced":
            return "cheap draft, premium when explicit"
        if candidate == "premium":
            return "prefer premium coding models"
        if candidate == "local":
            return "prefer local OpenAI-compatible models"
        return "routing mode"
    if command == "/set":
        return _SET_OPTION_DESCRIPTIONS.get(candidate, "")
    if command == "/show":
        if candidate in {"last", "."}:
            return "latest turn"
        if candidate.startswith("turn_"):
            return "turn projection"
        if candidate.startswith("item_"):
            return "item projection"
        return "projection target"
    if command == "/accept":
        return "pending command"
    if command == "/apply":
        return "pending file write"
    if command == "/diff":
        return "pending file write"
    if command == "/escalate":
        return "last turn escalation to reviewer"
    if command == "/reject":
        return "pending proposal"
    if command == "/inspect":
        return "workspace path"
    if command == "/run":
        raw = line.partition(" ")[2]
        parts = raw.split()
        if not parts or (len(parts) == 1 and not raw.endswith(" ")):
            return "shell command"
        return "workspace path"
    if command == "/task":
        return "task projection"
    if command == "/task-update":
        parts = line.split()
        if len(parts) <= 2:
            return "task id"
        return "task status"
    return ""


def _format_suggestion_lines(
    line: str,
    *,
    cwd: Path,
    store: JsonlStore,
    thread_id: str,
    color: bool,
    selected_index: int = 0,
) -> list[str]:
    items = _suggestion_items(
        line,
        cwd=cwd,
        store=store,
        thread_id=thread_id,
    )
    if not items:
        return []
    width = max(len(value) for value, _ in items)
    lines: list[str] = []
    for index, (value, description) in enumerate(items):
        selected = index == selected_index
        marker = ">" if selected else " "
        pad = " " * (width - len(value) + 2)
        value_style = "1;36" if selected else "36"
        description_style = "37" if selected else "2"
        display_marker = _ansi(marker, "1;36", color) if selected else marker
        display_value = _ansi(value, value_style, color)
        display_description = _ansi(description, description_style, color)
        lines.append(f"{display_marker} {display_value}{pad}{display_description}".rstrip())
    return lines


def _show_completion_candidates(store: JsonlStore, thread_id: str) -> list[str]:
    candidates = ["last", "."]
    try:
        view = load_thread_view(store, thread_id)
    except ValueError:
        return candidates

    for turn in view.turns:
        candidates.append(turn.id)
        for item in turn.items:
            candidates.append(item.id)
    return candidates


def _pending_command_completion_candidates(store: JsonlStore, thread_id: str) -> list[str]:
    return _pending_item_completion_candidates(store, thread_id, kind="command")


def _pending_file_write_completion_candidates(store: JsonlStore, thread_id: str) -> list[str]:
    return _pending_item_completion_candidates(store, thread_id, kind="file_write")


def _pending_proposal_completion_candidates(store: JsonlStore, thread_id: str) -> list[str]:
    return _pending_item_completion_candidates(
        store,
        thread_id,
        kind={"command", "file_write"},
    )


def _pending_item_completion_candidates(
    store: JsonlStore,
    thread_id: str,
    *,
    kind: str | set[str],
) -> list[str]:
    try:
        view = load_thread_view(store, thread_id)
    except ValueError:
        return []
    kinds = {kind} if isinstance(kind, str) else kind
    candidates: list[str] = []
    for turn in view.turns:
        for item in turn.items:
            if (
                item.kind in kinds
                and item.status == "pending"
                and item.approval == "requested"
            ):
                candidates.append(item.id)
    return candidates


def _task_item_completion_candidates(
    store: JsonlStore,
    thread_id: str,
) -> list[str]:
    try:
        view = load_thread_view(store, thread_id)
    except ValueError:
        return []
    candidates: list[str] = []
    for turn in view.turns:
        for item in turn.items:
            if item.kind == "task":
                candidates.append(item.id)
    return candidates


def _workspace_path_completion_candidates(cwd: Path, text: str) -> list[str]:
    scope = WorkspaceScope(cwd)
    raw_path = Path(text)
    raw_parent = raw_path.parent if raw_path.parent.as_posix() != "." else Path(".")
    name_prefix = raw_path.name

    try:
        parent = scope.resolve(raw_parent)
    except ValueError:
        return []
    if not parent.is_dir():
        return []

    candidates: list[str] = []
    try:
        children = sorted(parent.iterdir(), key=lambda path: path.name)
    except OSError:
        return []

    for child in children:
        relative = child.relative_to(scope.root).as_posix()
        if scope.is_ignored(relative):
            continue
        if not child.name.startswith(name_prefix):
            continue
        candidate = relative + "/" if child.is_dir() else relative
        candidates.append(candidate)
    return candidates


def _workspace_reference_completion_candidates(cwd: Path, text: str) -> list[str]:
    if not text.startswith("@"):
        return []
    return [
        f"@{candidate}"
        for candidate in _workspace_path_completion_candidates(cwd, text[1:])
    ]


def _run_completion_candidates(cwd: Path, line: str, text: str) -> list[str]:
    raw = line.partition(" ")[2]
    parts = raw.split()
    completing_new_arg = raw.endswith(" ")
    completing_command = not parts or (len(parts) == 1 and not completing_new_arg)
    if completing_command and "/" not in text:
        return _executable_completion_candidates(text)
    return _workspace_path_completion_candidates(cwd, text)


def _executable_completion_candidates(text: str, max_entries: int = 80) -> list[str]:
    candidates: set[str] = set()
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory)
        try:
            children = directory.iterdir()
        except OSError:
            continue
        for child in children:
            if len(candidates) >= max_entries:
                break
            name = child.name
            if not name.startswith(text):
                continue
            try:
                if child.is_file() and os.access(child, os.X_OK):
                    candidates.add(name)
            except OSError:
                continue
    return sorted(candidates)


def _create_native_composer(
    cwd: Path,
    store: JsonlStore,
    thread_id: str,
) -> _NativeComposer | None:
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
    except ImportError:
        return None
    return _NativeComposer(cwd, store, thread_id)


class _NativeComposer:
    def __init__(self, cwd: Path, store: JsonlStore, thread_id: str) -> None:
        self.cwd = cwd
        self.store = store
        self.thread_id = thread_id
        self.history_path = cwd / ".cocoa" / "input_history_plain"
        self.color = _should_use_color()

    def read(self, provider_status: str) -> str:
        try:
            import termios
            import tty
        except ImportError:
            return input(_format_repl_prompt(provider_status, self.thread_id, color=self.color))

        fd = sys.stdin.fileno()
        original_attrs = termios.tcgetattr(fd)
        history = self._load_history()
        history_index = len(history)
        buffer = ""
        cursor = 0
        rendered_lines = 0
        selected_index = 0
        previous_suggestions: tuple[str, ...] = ()
        pending_escape = ""

        try:
            tty.setcbreak(fd)
            rendered_lines = self._render(
                provider_status,
                buffer,
                rendered_lines,
                cursor=cursor,
                selected_index=selected_index,
            )
            while True:
                suggestions = self._suggestion_values(buffer, cursor)
                if suggestions != previous_suggestions:
                    selected_index = 0
                    previous_suggestions = suggestions
                if selected_index >= len(suggestions):
                    selected_index = 0
                key = _read_terminal_key()
                if pending_escape:
                    key = f"{pending_escape}{key}"
                    pending_escape = ""
                if key in {"\x1b", "\x1b["}:
                    pending_escape = key
                    continue
                if key.startswith("\x1b") and key not in _KNOWN_ESCAPE_KEYS:
                    key = "\x1b"
                if key in {"\x1b[A", "\x1b[B"}:
                    suggestions = self._suggestion_values(buffer, cursor)
                    if suggestions != previous_suggestions:
                        selected_index = 0
                        previous_suggestions = suggestions
                    if selected_index >= len(suggestions):
                        selected_index = 0
                if key in {"\r", "\n"}:
                    if suggestions and _completion_text(buffer[:cursor]) != suggestions[selected_index]:
                        buffer, cursor = _apply_completion_candidate_at_cursor(
                            buffer,
                            cursor,
                            suggestions[selected_index],
                        )
                        rendered_lines = self._render(
                            provider_status,
                            buffer,
                            rendered_lines,
                            cursor=cursor,
                            selected_index=selected_index,
                        )
                        continue
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    self._append_history(buffer)
                    return buffer
                if key == "\x03":
                    raise KeyboardInterrupt
                if key == "\x04":
                    if buffer:
                        continue
                    raise EOFError
                if key in {"\x7f", "\b"}:
                    if cursor > 0:
                        buffer = buffer[: cursor - 1] + buffer[cursor:]
                        cursor -= 1
                    history_index = len(history)
                elif key == "\x1b[3~":
                    if cursor < len(buffer):
                        buffer = buffer[:cursor] + buffer[cursor + 1:]
                    history_index = len(history)
                elif key in {"\x1b[D"}:
                    cursor = max(0, cursor - 1)
                elif key in {"\x1b[C"}:
                    cursor = min(len(buffer), cursor + 1)
                elif key in {"\x01", "\x1b[H", "\x1b[1~"}:
                    cursor = 0
                elif key in {"\x05", "\x1b[F", "\x1b[4~"}:
                    cursor = len(buffer)
                elif key == "\x15":
                    buffer = ""
                    cursor = 0
                    history_index = len(history)
                elif key == "\x17":
                    buffer, cursor = _delete_previous_word_at_cursor(buffer, cursor)
                    history_index = len(history)
                elif key == "\x0b":
                    buffer = buffer[:cursor]
                    history_index = len(history)
                elif key == "\t":
                    if suggestions:
                        buffer, cursor = _apply_completion_candidate_at_cursor(
                            buffer,
                            cursor,
                            suggestions[selected_index],
                        )
                    history_index = len(history)
                elif key == "\x1b[A":
                    if suggestions:
                        selected_index = (selected_index - 1) % len(suggestions)
                    elif history and history_index > 0:
                        history_index -= 1
                        buffer = history[history_index]
                        cursor = len(buffer)
                elif key == "\x1b[B":
                    if suggestions:
                        selected_index = (selected_index + 1) % len(suggestions)
                    elif history_index < len(history) - 1:
                        history_index += 1
                        buffer = history[history_index]
                        cursor = len(buffer)
                    elif history_index < len(history):
                        history_index = len(history)
                        buffer = ""
                        cursor = 0
                elif len(key) == 1 and key.isprintable():
                    buffer = buffer[:cursor] + key + buffer[cursor:]
                    cursor += 1
                    history_index = len(history)
                suggestions = self._suggestion_values(buffer, cursor)
                if suggestions != previous_suggestions:
                    selected_index = 0
                    previous_suggestions = suggestions
                if selected_index >= len(suggestions):
                    selected_index = 0
                rendered_lines = self._render(
                    provider_status,
                    buffer,
                    rendered_lines,
                    cursor=cursor,
                    selected_index=selected_index,
                )
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original_attrs)

    def _render(
        self,
        provider_status: str,
        line: str,
        previous_lines: int,
        *,
        cursor: int,
        selected_index: int,
    ) -> int:
        _clear_rendered_lines(previous_lines)
        lines = _format_repl_prompt(
            provider_status,
            self.thread_id,
            color=self.color,
        ).splitlines()
        input_prefix = lines[-1]
        lines[-1] = f"{input_prefix}{line}"
        lines.extend(
            _format_suggestion_lines(
                line[:cursor],
                cwd=self.cwd,
                store=self.store,
                thread_id=self.thread_id,
                color=self.color,
                selected_index=selected_index,
            )
        )
        sys.stdout.write("\r\n".join(lines))
        _move_cursor_to_input(
            suggestion_line_count=max(0, len(lines) - 2),
            input_prefix=input_prefix,
            text_before_cursor=line[:cursor],
        )
        sys.stdout.flush()
        return len(lines)

    def _suggestion_values(
        self,
        line: str,
        cursor: int,
    ) -> tuple[str, ...]:
        query = line[:cursor]
        return tuple(
            value
            for value, _ in _suggestion_items(
                query,
                cwd=self.cwd,
                store=self.store,
                thread_id=self.thread_id,
            )
        )

    def _load_history(self) -> list[str]:
        try:
            raw = self.history_path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [line for line in raw.splitlines() if line.strip()]

    def _append_history(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        history = self._load_history()
        if history and history[-1] == text:
            return
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError:
            return


def _read_terminal_key() -> str:
    key = sys.stdin.read(1)
    if key != "\x1b":
        return key
    try:
        import select
    except ImportError:
        return key
    parts = [key]
    while len(parts) < 6 and select.select([sys.stdin], [], [], 0.05)[0]:
        parts.append(sys.stdin.read(1))
    return "".join(parts)


def _clear_rendered_lines(line_count: int) -> None:
    if line_count <= 0:
        return
    sys.stdout.write("\r")
    if line_count > 1:
        sys.stdout.write("\033[F")
    for index in range(line_count):
        sys.stdout.write("\033[K")
        if index < line_count - 1:
            sys.stdout.write("\033[E")
    if line_count > 1:
        sys.stdout.write(f"\033[{line_count - 1}F")
    sys.stdout.write("\r")


def _delete_previous_word(line: str) -> str:
    stripped = line.rstrip()
    if not stripped:
        return ""
    index = stripped.rfind(" ")
    if index < 0:
        return ""
    return stripped[: index + 1]


def _delete_previous_word_at_cursor(line: str, cursor: int) -> tuple[str, int]:
    before_cursor = line[:cursor].rstrip()
    if not before_cursor:
        return line[cursor:], 0
    index = before_cursor.rfind(" ")
    new_cursor = 0 if index < 0 else index + 1
    after_cursor = line[cursor:]
    if new_cursor > 0 and line[:new_cursor].endswith(" ") and after_cursor.startswith(" "):
        after_cursor = after_cursor[1:]
    return line[:new_cursor] + after_cursor, new_cursor


def _move_cursor_to_input(
    *,
    suggestion_line_count: int,
    input_prefix: str,
    text_before_cursor: str,
) -> None:
    if suggestion_line_count > 0:
        sys.stdout.write(f"\033[{suggestion_line_count}F")
    sys.stdout.write("\r")
    column = _display_width(input_prefix) + _display_width(text_before_cursor)
    if column > 0:
        sys.stdout.write(f"\033[{column}C")


def _display_width(text: str) -> int:
    plain = _ANSI_RE.sub("", text)
    width = 0
    for character in plain:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def _create_prompt_toolkit_session(
    cwd: Path,
    store: JsonlStore,
    thread_id: str,
) -> Any | None:
    try:
        from prompt_toolkit import PromptSession  # type: ignore[import-not-found]
        from prompt_toolkit.completion import Completer, Completion  # type: ignore[import-not-found]
        from prompt_toolkit.history import FileHistory  # type: ignore[import-not-found]
        from prompt_toolkit.styles import Style  # type: ignore[import-not-found]
    except ImportError:
        return None

    class CocoaCompleter(Completer):
        def get_completions(
            self,
            document: object,
            complete_event: object,
        ) -> Iterator[object]:
            text_before = getattr(document, "text_before_cursor", "")
            word = _completion_text(str(text_before))
            for candidate in _completion_candidates(
                str(text_before),
                word,
                cwd=cwd,
                store=store,
                thread_id=thread_id,
            ):
                yield Completion(candidate, start_position=-len(word))

    history_path = cwd / ".cocoa" / "input_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        history=FileHistory(str(history_path)),
        completer=CocoaCompleter(),
        complete_while_typing=True,
        style=Style.from_dict(
            {
                "box": "#6b7280",
                "title": "bold #f8fafc",
                "meta": "#94a3b8",
                "prompt": "bold #38bdf8",
                "toolbar": "bg:#111827 #94a3b8",
                "toolbar.good": "bg:#111827 #86efac bold",
                "toolbar.warn": "bg:#111827 #facc15 bold",
            }
        ),
    )


def _prompt_toolkit_fragments(provider_status: str, thread_id: str) -> list[tuple[str, str]]:
    return [
        ("class:box", "+-- "),
        ("class:title", "cocoa"),
        ("class:meta", f"  {provider_status}  {thread_id}\n"),
        ("class:prompt", "+> "),
    ]


def _prompt_toolkit_toolbar(provider_status: str) -> list[tuple[str, str]]:
    style = "class:toolbar.good" if _provider_ready_status(provider_status) else "class:toolbar.warn"
    return [
        ("class:toolbar", " Tab completes  "),
        ("class:toolbar", " /help commands  "),
        (style, f" {provider_status} "),
    ]


def _format_repl_prompt(provider_status: str, thread_id: str, *, color: bool) -> str:
    title = _ansi("cocoa", "1;36", color)
    meta = _ansi(f"{provider_status}  {thread_id}", "2", color)
    prompt = _ansi("+> ", "1;36", color)
    rule = _ansi("+--", "2", color)
    return f"{rule} {title}  {meta}\n{prompt}"


def _provider_ready_status(provider_status: str) -> bool:
    return provider_status != "stub" and not provider_status.startswith("not configured (")


def _should_use_color() -> bool:
    return sys.stdin.isatty() and os.environ.get("NO_COLOR") is None


def _ansi(text: str, code: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"\033[{code}m{text}\033[0m"


def _install_repl_completion(
    cwd: Path,
    store: JsonlStore,
    thread_id: str,
) -> Callable[[], None]:
    try:
        import readline
    except ImportError:
        return lambda: None

    previous_completer = readline.get_completer()
    previous_delims = readline.get_completer_delims()

    def completer(text: str, state: int) -> str | None:
        line = readline.get_line_buffer()
        matches = _completion_candidates(
            line,
            text,
            cwd=cwd,
            store=store,
            thread_id=thread_id,
        )
        try:
            return matches[state]
        except IndexError:
            return None

    readline.set_completer(completer)
    readline.set_completer_delims(" \t\n")
    readline.parse_and_bind("tab: complete")

    def restore() -> None:
        readline.set_completer(previous_completer)
        readline.set_completer_delims(previous_delims)

    return restore


def _resolve_provider_status(env: Mapping[str, str] | None = None) -> str:
    # keep backwards compatibility for callers that pass no env
    source = dict(os.environ) if env is None else dict(env)
    return _resolve_provider_status_for_env(source)


def _resolve_provider_status_for_env(env: Mapping[str, str]) -> str:
    try:
        return provider_name_from_env(env)
    except ProviderConfigurationError as exc:
        return f"not configured ({exc})"


def _resolve_routed_provider_status_for_env(env: Mapping[str, str]) -> str:
    provider_env, _ = _provider_environment_for_mode(env)
    return _resolve_provider_status_for_env(provider_env)


def _resolve_provider_model(env: Mapping[str, str] | None = None) -> str:
    status = _resolve_provider_status(env)
    if status == "stub" or status.startswith("not configured ("):
        return "unknown"
    index = status.find(":")
    if index >= 0:
        return status[index + 1 :]
    return "default"


def _resolve_provider_model_for_env(env: Mapping[str, str]) -> str:
    status = _resolve_routed_provider_status_for_env(env)
    if status == "stub" or status.startswith("not configured ("):
        return "unknown"
    index = status.find(":")
    if index >= 0:
        return status[index + 1 :]
    return "default"


def _is_provider_configured(env: Mapping[str, str] | None = None) -> bool:
    if env is None:
        status = _resolve_provider_status()
    else:
        status = _resolve_routed_provider_status_for_env(env)
    return status != "stub" and not status.startswith("not configured (")


def _routing_payload_for_env(env: Mapping[str, str]) -> dict[str, Any]:
    provider_env, profile_used = _provider_environment_for_mode(env)
    status = _resolve_provider_status_for_env(provider_env)
    mode = _resolve_model_mode(env)
    provider = status
    model: str | None = None
    if ":" in status:
        provider, model = status.split(":", 1)
    elif status in {"stub"} or status.startswith("not configured ("):
        provider = status
    reason = f"mode={mode}"
    if profile_used:
        reason += " profile override"
    else:
        reason += " default provider"
    payload: dict[str, Any] = {
        "mode": mode,
        "provider": provider,
        "status": status,
        "profile": mode if profile_used else "default",
        "reason": reason,
    }
    if model:
        payload["model"] = model
    return payload


def _routing_payload_from_config(cwd: Path, overrides: Mapping[str, str]) -> dict[str, Any]:
    return cocoa_config.config_to_routing_payload(cwd, overrides)


def _turn_preview(turn: TurnView) -> str:
    for item in reversed(turn.items):
        if item.kind in {"agent_message", "user_message"}:
            text = item.content.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    if turn.errors:
        return turn.errors[-1].get("message", "")
    return ""


def _item_preview(item: ItemView) -> str:
    if item.kind in {"agent_message", "user_message"}:
        text = item.content.get("text")
        if isinstance(text, str):
            return textwrap.shorten(text.replace("\n", " "), width=90)
    if item.kind == "command":
        command = item.content.get("command")
        exit_code = item.content.get("exit_code")
        if isinstance(command, str):
            suffix = f" exit={exit_code}" if exit_code is not None else ""
            return textwrap.shorten(command + suffix, width=90)
    if item.kind == "file_read":
        path = item.content.get("path")
        size = item.content.get("size")
        if isinstance(path, str):
            suffix = f" ({size} bytes)" if isinstance(size, int) else ""
            return textwrap.shorten(f"read {path}{suffix}", width=90)
    if item.kind == "workspace_inspect":
        path = item.content.get("path")
        entries = item.content.get("entries")
        count = len(entries) if isinstance(entries, list) else 0
        if isinstance(path, str):
            return textwrap.shorten(f"inspect {path} ({count} entries)", width=90)
    rendered = json.dumps(item.content, ensure_ascii=False, sort_keys=True)
    return textwrap.shorten(rendered, width=90)


def _print_turn_view(turn: TurnView) -> None:
    print(f"turn: {turn.id}")
    print(f"status: {turn.status}")
    print(f"intent: {turn.intent}")
    if turn.summary:
        print(f"summary: {turn.summary}")
    if turn.errors:
        print("errors:")
        for error in turn.errors:
            print(f"  {error.get('type', 'Error')}: {error.get('message', '')}")
    if not turn.items:
        print("items: none")
        return
    print("items:")
    for item in turn.items:
        print(
            f"  {item.id}\t{item.kind}\t{item.status}\t"
            f"approval={item.approval}\t{_item_preview(item)}"
        )


def _print_item_view(item: ItemView) -> None:
    print(f"item: {item.id}")
    print(f"turn: {item.turn_id}")
    print(f"kind: {item.kind}")
    print(f"status: {item.status}")
    print(f"approval: {item.approval}")
    print("content:")
    print(json.dumps(item.content, ensure_ascii=False, indent=2, sort_keys=True))



def _print_provider_config_help() -> None:
    print("provider is not configured.")
    print("Run one of these in current session (persisted by /configure):")
    print("")
    print("# OpenAI-compatible")
    print("/configure openai <YOUR_KEY> gpt-5 [base_url]")
    print("")
    print("# Codex HTTP (if logged in ChatGPT)")
    print("/configure codex-http [model]")
    print("")
    print("# Session override")
    print("/set --persist KEY VALUE")


def _print_context_items(items: tuple[ItemRecord, ...]) -> None:
    if not items:
        return
    print()
    print("context:")
    for item in items:
        source = item.content.get("source")
        if source != "prompt_reference":
            continue
        if item.kind == ItemKind.FILE_READ:
            path = item.content.get("path")
            if item.status == "failed":
                error = item.content.get("error")
                print(f"  {item.id}: read {path} failed")
                if isinstance(error, str) and error:
                    print(f"    error: {error}")
                continue
            size = item.content.get("size")
            truncated = item.content.get("truncated")
            suffix = f" ({size} bytes)" if isinstance(size, int) else ""
            print(f"  {item.id}: read {path}{suffix}")
            if truncated is True:
                print("    truncated for model context")
            continue
        if item.kind == ItemKind.WORKSPACE_INSPECT:
            path = item.content.get("path")
            entries = item.content.get("entries")
            count = len(entries) if isinstance(entries, list) else 0
            print(f"  {item.id}: inspect {path} ({count} entries)")


def _pending_proposal_views(store: JsonlStore, thread_id: str) -> list[ItemView]:
    try:
        view = load_thread_view(store, thread_id)
    except ValueError:
        return []
    pending: list[ItemView] = []
    for turn in view.turns:
        for item in turn.items:
            if (
                item.kind in {"command", "file_write"}
                and item.status == "pending"
                and item.approval == "requested"
            ):
                pending.append(item)
    return pending


def _print_file_proposal_recovery_hint(item: ItemRecord | ItemView) -> None:
    operation = item.content.get("operation")
    path = item.content.get("path")
    if operation == "replace" and isinstance(path, str):
        print(
            f"    hint: regenerate this edit with fresh @{path} context; "
            "the proposed old text must match the current file exactly."
        )
        return
    print("    hint: reject this proposal and ask cocoa to regenerate it from current context.")


def _print_pending_proposals(items: list[ItemView]) -> None:
    if not items:
        print("no pending proposals")
        return
    print("pending proposals:")
    for item in items:
        if item.kind == "command":
            command = item.content.get("command")
            reason = item.content.get("reason")
            print(f"  {item.id}: {textwrap.shorten(str(command), width=90)}")
            if isinstance(reason, str) and reason:
                print(f"    reason: {reason}")
            print(f"    run: /accept {item.id}")
            print(f"    reject: /reject {item.id}")
            continue
        if item.kind == "file_write":
            path = item.content.get("path")
            operation = item.content.get("operation", "write")
            action = "edit" if operation == "replace" else "write"
            reason = item.content.get("reason")
            scope_error = item.content.get("scope_error")
            print(f"  {item.id}: {action} {path}")
            if isinstance(reason, str) and reason:
                print(f"    reason: {reason}")
            if isinstance(scope_error, str) and scope_error:
                print(f"    cannot apply: {scope_error}")
                _print_file_proposal_recovery_hint(item)
            else:
                print(f"    diff: /diff {item.id}")
                print(f"    apply: /apply {item.id}")
            print(f"    reject: /reject {item.id}")


def _print_pending_diff(store: JsonlStore, thread_id: str, item_id: str) -> None:
    try:
        view = load_thread_view(store, thread_id)
    except ValueError as exc:
        print(str(exc))
        return
    item = view.find_item(item_id)
    if item is None:
        print(f"not found: {item_id}")
        return
    if item.kind != "file_write":
        print(f"item is not a file write proposal: {item_id}")
        return
    if item.status != "pending" or item.approval != "requested":
        print(f"file write proposal is not pending: {item_id}")
        return
    scope_error = item.content.get("scope_error")
    if isinstance(scope_error, str) and scope_error:
        print(f"cannot show diff: {scope_error}")
        _print_file_proposal_recovery_hint(item)
        print(f"reject: /reject {item.id}")
        return
    diff = item.content.get("diff")
    if not isinstance(diff, str) or not diff:
        print(f"no diff available: {item_id}")
        return
    print(diff, end="" if diff.endswith("\n") else "\n")


def _print_proposals(proposals: tuple[ItemRecord, ...]) -> None:
    if not proposals:
        return
    print()
    print("proposals:")
    for item in proposals:
        if item.kind == ItemKind.COMMAND:
            command = item.content.get("command")
            reason = item.content.get("reason")
            print(f"  {item.id}: {command}")
            if isinstance(reason, str) and reason:
                print(f"    reason: {reason}")
            print(f"    run: /accept {item.id}")
            print(f"    reject: /reject {item.id}")
            continue
        if item.kind == ItemKind.FILE_WRITE:
            path = item.content.get("path")
            operation = item.content.get("operation", "write")
            action = "edit" if operation == "replace" else "write"
            reason = item.content.get("reason")
            scope_error = item.content.get("scope_error")
            print(f"  {item.id}: {action} {path}")
            if isinstance(reason, str) and reason:
                print(f"    reason: {reason}")
            if isinstance(scope_error, str) and scope_error:
                print(f"    cannot apply: {scope_error}")
                _print_file_proposal_recovery_hint(item)
            else:
                diff = item.content.get("diff")
                if isinstance(diff, str) and diff:
                    print(textwrap.indent(diff.rstrip(), "    "))
                    print(f"    diff: /diff {item.id}")
                print(f"    apply: /apply {item.id}")
            print(f"    reject: /reject {item.id}")


async def run_ask(cwd: Path, prompt: str, thread_id: str | None = None) -> None:
    runtime, store = make_runtime(cwd)
    env = _effective_environment(cwd)
    if thread_id is None:
        thread = runtime.start_thread(cwd, title=prompt[:80])
    else:
        thread = runtime.resume_thread(thread_id)
    result = await runtime.run_user_turn_with_result(
        thread,
        prompt,
        routing=_routing_payload_for_env(env),
    )
    _print_context_items(result.context_items)
    print(result.message)
    _print_proposals(result.proposals)
    print()
    print(f"thread: {thread.id}")
    print(f"log: {store.thread_path(thread.id)}")


async def run_repl(cwd: Path, thread_id: str | None = None) -> None:
    overrides: dict[str, str] = {}
    runtime, store, provider_status = _resolve_runtime_from_env(cwd, overrides)

    def print_provider_status() -> None:
        print(f"provider: {_resolve_routed_provider_status_for_env(_effective_session_environment(cwd, overrides))}")

    def rebuild_runtime() -> None:
        nonlocal runtime, provider_status
        runtime, _, provider_status = _resolve_runtime_from_env(cwd, overrides)

    def set_and_reload_env(key: str, value: str) -> None:
        overrides[key] = value
        rebuild_runtime()

    env = _effective_session_environment(cwd, overrides)
    if thread_id is None:
        thread = runtime.start_thread(cwd, title="repl")
    else:
        thread = runtime.resume_thread(thread_id)
    shell = ShellTool(ConsoleApprovalPrompter())

    print(f"cocoa {__version__}")
    print(f"thread: {thread.id}")
    print(f"provider: {provider_status}")
    if not _is_provider_configured(env):
        print("provider not ready, type /configure for setup, /help for commands")
    print("type /help for commands, /exit to quit")

    repl_input = ReplInput(cwd, store, thread.id)
    try:
        while True:
            try:
                line = repl_input.read(provider_status).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not line:
                continue
            if line in {"/exit", "/quit"}:
                break
            if line == "/help":
                print("/help               show commands")
                print("/status             show session status")
                print("/provider           show provider status")
                print("/model              show model selection")
                print("/mode [name]        show or set routing mode")
                print("/configure          configure provider in session or save config")
                print("/set [--persist|-p] KEY VALUE")
                print("                    set session variable (and optionally persist)")
                print("/persist            persist current session overrides to .cocoa/cocoa.toml")
                print("/history            show turns in current thread")
                print("/usage              show model usage in current thread")
                print("/show <id|last>     show a turn or item projection")
                print("/tasks              list current tasks")
                print("/task <id>          show task details")
                print("/task-add <subject> -- [description]")
                print("                    create a new task")
                print("/task-update <id> --status <status>")
                print("                    update task status")
                print("/pending            show pending proposals")
                print("/diff <item_id>     show pending file write diff")
                print("/escalate last      escalate previous turn to reviewer/planner")
                print("/accept <item_id>   run a pending command proposal")
                print("/apply <item_id>    apply a pending file write proposal")
                print("/reject <item_id>   reject a pending proposal")
                print("/inspect [path]     list workspace files")
                print("/run <command>      run shell command after approval")
                print("@path               include file or directory context in a prompt")
                print("/exit               quit")
                continue
            if line == "/configure":
                _print_provider_config_help()
                continue
            if line == "/mode" or line.startswith("/mode "):
                _, _, raw_mode = line.partition(" ")
                mode = raw_mode.strip().lower()
                if not mode:
                    print(f"mode: {_resolve_model_mode(env)}")
                    print("available: " + " | ".join(_MODEL_MODES))
                    continue
                if mode not in _MODEL_MODES:
                    print("usage: /mode cheap|balanced|premium|local")
                    continue
                set_and_reload_env(_MODEL_MODE_ENV, mode)
                env = _effective_session_environment(cwd, overrides)
                print(f"mode: {mode}")
                print_provider_status()
                continue
            if line.startswith("/configure "):
                _, _, raw = line.partition(" ")
                args = shlex.split(raw)
                if not args:
                    print("usage: /configure <openai|codex-http|codex> <args...>")
                    continue
                mode = args[0].lower()
                if mode == "openai":
                    if len(args) < 3:
                        print("usage: /configure openai <api_key> <model> [base_url]")
                        continue
                    data = cocoa_config.load_workspace_config(cwd)
                    data["provider_used"] = "openai"
                    cocoa_config.set_dotted(data, "provider.openai.api_key", args[1])
                    cocoa_config.set_dotted(data, "provider.openai.model", args[2])
                    if len(args) > 3:
                        cocoa_config.set_dotted(data, "provider.openai.base_url", args[3])
                    cocoa_config.save_workspace_config(cwd, data)
                    overrides.clear()
                    rebuild_runtime()
                    env = _effective_session_environment(cwd, overrides)
                    print("provider config persisted to .cocoa/cocoa.toml")
                    print_provider_status()
                    continue
                if mode == "codex-http":
                    data = cocoa_config.load_workspace_config(cwd)
                    data["provider_used"] = "codex-http"
                    if len(args) > 1:
                        cocoa_config.set_dotted(data, "provider.codex.model", args[1])
                        if len(args) > 2:
                            cocoa_config.set_dotted(data, "provider.codex.api_key", args[2])
                    cocoa_config.save_workspace_config(cwd, data)
                    overrides.clear()
                    rebuild_runtime()
                    env = _effective_session_environment(cwd, overrides)
                    print("provider config persisted to .cocoa/cocoa.toml")
                    print_provider_status()
                    continue
                if mode == "clear":
                    data = cocoa_config.load_workspace_config(cwd)
                    data.pop("provider_used", None)
                    data.pop("provider", None)
                    cocoa_config.save_workspace_config(cwd, data)
                    overrides.clear()
                    rebuild_runtime()
                    env = _effective_session_environment(cwd, overrides)
                    print("provider override cleared in workspace config")
                    continue
                print("unknown /configure mode. use openai | codex-http | clear")
                continue
            if line.startswith("/set "):
                _, _, raw = line.partition(" ")
                try:
                    tokens = shlex.split(raw)
                except ValueError:
                    print("invalid quoting in command")
                    continue
                persist = False
                if not tokens:
                    print("usage: /set [--persist|-p] KEY VALUE")
                    continue
                if tokens[0] in {"-p", "--persist"}:
                    persist = True
                    tokens = tokens[1:]
                if not tokens:
                    print("usage: /set [--persist|-p] KEY VALUE")
                    continue
                if "=" in tokens[0] and len(tokens) == 1:
                    key, value = tokens[0].split("=", 1)
                elif len(tokens) >= 2:
                    key = tokens[0]
                    value = " ".join(tokens[1:])
                else:
                    print("usage: /set [--persist|-p] KEY VALUE")
                    continue
                if not key:
                    print("missing variable name")
                    continue
                set_and_reload_env(key, value)
                env = _effective_session_environment(cwd, overrides)
                if persist:
                    _persist_environment(cwd, {key: value})
                print(
                    f"{key} {'persisted and ' if persist else ''}set"
                )
                print_provider_status()
                continue
            if line == "/persist":
                if not overrides:
                    print("no session overrides to persist")
                    continue
                data = cocoa_config.load_workspace_config(cwd)
                data = cocoa_config.merge_session_overrides(data, overrides)
                cocoa_config.save_workspace_config(cwd, data)
                print("session overrides persisted to .cocoa/cocoa.toml")
                continue
            if line == "/status":
                print(f"thread: {thread.id}")
                print(f"cwd: {cwd}")
                print(f"log: {store.thread_path(thread.id)}")
                print(f"mode: {_resolve_model_mode(env)}")
                print(f"provider: {_resolve_routed_provider_status_for_env(env)}")
                continue
            if line == "/history":
                print_history(store, thread.id)
                continue
            if line == "/usage":
                _print_usage(store, thread.id)
                continue
            if line == "/pending":
                _print_pending_proposals(_pending_proposal_views(store, thread.id))
                continue
            if line == "/show" or line.startswith("/show "):
                _, _, target = line.partition(" ")
                target = target.strip()
                if not target:
                    print("usage: /show <turn_id|item_id|last>")
                    continue
                print_show(store, thread.id, target)
                continue
            if line == "/tasks":
                tasks = runtime.list_tasks(thread)
                if not tasks:
                    print("no tasks")
                    continue
                print("tasks:")
                for t in tasks:
                    t_status = t.content.get("status", "unknown")
                    t_subject = t.content.get("subject", "-")
                    print(f"  {t.id}\t{t_status}\t{t_subject}")
                continue
            if line == "/task":
                print("usage: /task <id>")
                continue
            if line.startswith("/task "):
                _, _, target = line.partition(" ")
                target = target.strip()
                if not target:
                    print("usage: /task <id>")
                    continue
                view = load_thread_view(store, thread.id)
                item = view.find_item(target)
                if item is None or item.kind != "task":
                    print(f"task not found: {target}")
                    continue
                _print_item_view(item)
                continue
            if line.startswith("/task-add "):
                raw = line.removeprefix("/task-add ").strip()
                if " -- " in raw:
                    subject, description = raw.split(" -- ", 1)
                else:
                    subject = raw
                    description = ""
                subject = subject.strip()
                if not subject:
                    print("usage: /task-add <subject> -- [description]")
                    continue
                try:
                    created_task = runtime.create_task(thread, subject, description=description)
                except ValueError as exc:
                    print(str(exc))
                    continue
                print(f"created: {created_task.id}")
                print(f"subject: {subject}")
                continue
            if line.startswith("/task-update "):
                raw = line.removeprefix("/task-update ").strip()
                parts = shlex.split(raw)
                if not parts:
                    print("usage: /task-update <id> --status <status>")
                    continue
                item_id = parts[0]
                status: str | None = None
                for i, part in enumerate(parts[1:], 1):
                    if part == "--status" and i + 1 < len(parts):
                        status = parts[i + 1]
                        break
                if not status:
                    print("usage: /task-update <id> --status <status>")
                    print("available: pending | in_progress | completed")
                    continue
                try:
                    updated_task = runtime.update_task(thread, item_id, status=status)
                except ValueError as exc:
                    print(str(exc))
                    continue
                print(f"updated: {updated_task.id}")
                print(f"status: {status}")
                continue
            if line == "/diff" or line.startswith("/diff "):
                _, _, target = line.partition(" ")
                target = target.strip()
                if not target:
                    print("usage: /diff <item_id>")
                    continue
                _print_pending_diff(store, thread.id, target)
                continue
            if line == "/accept" or line.startswith("/accept "):
                _, _, target = line.partition(" ")
                target = target.strip()
                if not target:
                    print("usage: /accept <item_id>")
                    continue
                try:
                    result = await runtime.run_proposed_command(thread, target, shell)
                except ValueError as exc:
                    print(str(exc))
                    continue
                if result.stdout:
                    print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
                if result.stderr:
                    print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")
                if result.exit_code is not None:
                    print(f"exit_code: {result.exit_code}")
                continue
            if line == "/apply" or line.startswith("/apply "):
                _, _, target = line.partition(" ")
                target = target.strip()
                if not target:
                    print("usage: /apply <item_id>")
                    continue
                try:
                    applied_item = runtime.apply_proposed_file_write(thread, target)
                except ValueError as exc:
                    print(f"cannot apply: {exc}")
                    try:
                        apply_view = load_thread_view(store, thread.id)
                    except ValueError:
                        apply_view = None
                    if apply_view is not None:
                        pending_item = apply_view.find_item(target)
                        if pending_item is not None and pending_item.kind == "file_write":
                            _print_file_proposal_recovery_hint(pending_item)
                            print(f"reject: /reject {pending_item.id}")
                    continue
                path = applied_item.content.get("path")
                bytes_written = applied_item.content.get("bytes_written")
                print(f"applied: {path}")
                if isinstance(bytes_written, int):
                    print(f"bytes_written: {bytes_written}")
                continue
            if line == "/reject" or line.startswith("/reject "):
                _, _, target = line.partition(" ")
                target = target.strip()
                if not target:
                    print("usage: /reject <item_id>")
                    continue
                try:
                    rejected_item = runtime.reject_pending_item(thread, target)
                except ValueError as exc:
                    print(str(exc))
                    continue
                path = rejected_item.content.get("path")
                command_text = rejected_item.content.get("command")
                label = path if rejected_item.kind == ItemKind.FILE_WRITE else command_text
                print(f"rejected: {rejected_item.id}")
                if isinstance(label, str) and label:
                    print(f"target: {label}")
                continue
            if line == "/provider":
                print(f"provider: {_resolve_routed_provider_status_for_env(env)}")
                continue
            if line == "/model":
                print(f"model: {_resolve_provider_model_for_env(env)}")
                continue
            if line.startswith("/inspect"):
                _, _, raw_path = line.partition(" ")
                print_inspect(cwd, raw_path or ".", max_entries=80)
                continue
            if line.startswith("/escalate "):
                _, _, raw_target = line.partition(" ")
                target = raw_target.strip()
                if target != "last":
                    print("usage: /escalate last")
                    continue
                env = _effective_session_environment(cwd, overrides)
                mode = _resolve_model_mode(env)
                if mode != "premium":
                    print("hint: use /mode premium for Codex review")
                try:
                    escalation_result = await runtime.run_escalation_turn(
                        thread,
                        target="last",
                        routing=_routing_payload_for_env(env),
                    )
                except ValueError as exc:
                    print(str(exc))
                    continue
                print(escalation_result.message)
                continue
            if line.startswith("/run "):
                command = line.removeprefix("/run ").strip()
                if not command:
                    print("missing command")
                    continue
                shell_result = await runtime.run_shell_turn(thread, command, shell)
                if shell_result.stdout:
                    print(shell_result.stdout, end="" if shell_result.stdout.endswith("\n") else "\n")
                if shell_result.stderr:
                    print(shell_result.stderr, end="" if shell_result.stderr.endswith("\n") else "\n")
                if shell_result.exit_code is not None:
                    print(f"exit_code: {shell_result.exit_code}")
                continue
            if line.startswith("/"):
                print("unknown command. type /help for commands.")
                continue

            turn_result = await runtime.run_user_turn_with_result(
                thread,
                line,
                routing=_routing_payload_for_env(env),
            )
            _print_context_items(turn_result.context_items)
            print(turn_result.message)
            _print_proposals(turn_result.proposals)
    finally:
        repl_input.close()
    print(f"log: {store.thread_path(thread.id)}")


def print_threads(cwd: Path) -> None:
    store = JsonlStore.for_workspace(cwd)
    logs = store.list_threads()
    if not logs:
        print("no threads")
        return
    for log in logs:
        print(f"{log.thread_id}\t{log.event_count}\t{log.path}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        args.command = "repl"
    cwd = resolve_cwd(args.cwd)

    try:
        if args.command == "doctor":
            print_doctor(cwd)
            return 0
        if args.command == "inspect":
            print_inspect(cwd, args.path, args.max)
            return 0
        if args.command == "ask":
            asyncio.run(run_ask(cwd, args.prompt, args.thread))
            return 0
        if args.command == "repl":
            asyncio.run(run_repl(cwd, args.thread))
            return 0
        if args.command == "threads":
            print_threads(cwd)
            return 0
    except ValueError as exc:
        print(f"invalid thread: {exc}", file=sys.stderr)
        return 3
    except ProviderConfigurationError as exc:
        print(f"provider configuration error: {exc}", file=sys.stderr)
        return 2

    raise SystemExit(f"unknown command: {args.command}")
