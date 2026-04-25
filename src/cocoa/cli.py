from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from . import __version__
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


def _load_config_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        return _parse_config_lines(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _effective_environment(cwd: Path) -> dict[str, str]:
    home = Path.home()
    env = dict(os.environ)
    env.update(_load_config_env(home / ".cocoa" / "config.env"))
    env.update(_load_config_env(cwd / ".cocoa" / "config.env"))
    return env


def _serialize_env_value(value: str) -> str:
    if value == "":
        return "\"\""
    if any(ch.isspace() for ch in value):
        return shlex.quote(value)
    return value


def _persist_environment(cwd: Path, updates: Mapping[str, str]) -> None:
    path = cwd / ".cocoa" / "config.env"
    existing = _load_config_env(path)
    existing.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{key}={_serialize_env_value(value)}" for key, value in sorted(existing.items())
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _effective_session_environment(
    cwd: Path,
    overrides: Mapping[str, str],
) -> dict[str, str]:
    env = _effective_environment(cwd)
    env.update(overrides)
    return env


def _resolve_runtime_from_env(cwd: Path, overrides: Mapping[str, str]) -> tuple[
    AgentRuntime,
    JsonlStore,
    str,
]:
    env = _effective_session_environment(cwd, overrides)
    store = JsonlStore.for_workspace(cwd)
    try:
        provider_status = _resolve_provider_status_for_env(env)
        provider = provider_from_env(env)
    except ProviderConfigurationError as exc:
        provider_status = f"not configured ({exc})"
        provider = StubProvider()
    return AgentRuntime(store=store, provider=provider), store, provider_status


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
    return AgentRuntime(store=store, provider=provider_from_env(env)), store


def print_doctor(cwd: Path) -> None:
    try:
        provider_name = provider_name_from_env(_effective_environment(cwd))
    except ProviderConfigurationError as exc:
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
    print(f"provider: {provider_name}")


def print_inspect(cwd: Path, path: str, max_entries: int) -> None:
    scope = WorkspaceScope(cwd)
    entries = scope.inspect(path, max_entries=max_entries)
    for entry in entries:
        print(f"{entry.path}\t{entry.size}")
    if len(entries) >= max_entries:
        print(f"... capped at {max_entries} entries")


def _resolve_provider_status(env: Mapping[str, str] | None = None) -> str:
    # keep backwards compatibility for callers that pass no env
    source = dict(os.environ) if env is None else dict(env)
    return _resolve_provider_status_for_env(source)


def _resolve_provider_status_for_env(env: Mapping[str, str]) -> str:
    try:
        return provider_name_from_env(env)
    except ProviderConfigurationError as exc:
        return f"not configured ({exc})"


def _resolve_provider_model(env: Mapping[str, str] | None = None) -> str:
    status = _resolve_provider_status(env)
    if status == "stub" or status.startswith("not configured ("):
        return "unknown"
    index = status.find(":")
    if index >= 0:
        return status[index + 1 :]
    return "default"


def _resolve_provider_model_for_env(env: Mapping[str, str]) -> str:
    status = _resolve_provider_status_for_env(env)
    if status == "stub" or status.startswith("not configured ("):
        return "unknown"
    index = status.find(":")
    if index >= 0:
        return status[index + 1 :]
    return "default"


def _is_provider_configured(env: Mapping[str, str] | None = None) -> bool:
    status = _resolve_provider_status(env)
    return status != "stub" and not status.startswith("not configured (")



def _print_provider_config_help() -> None:
    print("provider is not configured.")
    print("Run one of these and restart cocoa:")
    print("")
    print("# OpenAI-compatible")
    print("export COCOA_PROVIDER=openai")
    print("export COCOA_OPENAI_API_KEY=\"<YOUR_KEY>\"")
    print("export COCOA_OPENAI_MODEL=\"gpt-5\"")
    print("")
    print("# Codex HTTP (if logged in ChatGPT)")
    print("export COCOA_PROVIDER=codex-http")
    print("export COCOA_CODEX_BASE_URL=\"https://chatgpt.com/backend-api/codex\"")
    print("python -m cocoa")


async def run_ask(cwd: Path, prompt: str, thread_id: str | None = None) -> None:
    runtime, store = make_runtime(cwd)
    if thread_id is None:
        thread = runtime.start_thread(cwd, title=prompt[:80])
    else:
        thread = runtime.resume_thread(thread_id)
    message = await runtime.run_user_turn(thread, prompt)
    print(message)
    print()
    print(f"thread: {thread.id}")
    print(f"log: {store.thread_path(thread.id)}")


async def run_repl(cwd: Path, thread_id: str | None = None) -> None:
    overrides: dict[str, str] = {}
    runtime, store, provider_status = _resolve_runtime_from_env(cwd, overrides)

    def print_provider_status() -> None:
        print(f"provider: {_resolve_provider_status_for_env(_effective_session_environment(cwd, overrides))}")

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

    while True:
        try:
            line = input("cocoa> ").strip()
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
            print("/configure          configure provider in session or save config")
            print("/set KEY VALUE      set session variable and apply immediately")
            print("/inspect [path]     list workspace files")
            print("/run <command>      run shell command after approval")
            print("/exit               quit")
            continue
        if line == "/configure":
            _print_provider_config_help()
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
                updates = {"COCOA_PROVIDER": "openai", "COCOA_OPENAI_API_KEY": args[1], "COCOA_OPENAI_MODEL": args[2]}
                if len(args) > 3:
                    updates["COCOA_OPENAI_BASE_URL"] = args[3]
                _persist_environment(cwd, updates)
                overrides.clear()
                rebuild_runtime()
                env = _effective_session_environment(cwd, overrides)
                print("provider config persisted to .cocoa/config.env")
                print_provider_status()
                continue
            if mode == "codex-http":
                updates = {"COCOA_PROVIDER": "codex-http"}
                if len(args) > 1:
                    updates["COCOA_CODEX_MODEL"] = args[1]
                    if len(args) > 2:
                        updates["COCOA_CODEX_API_KEY"] = args[2]
                _persist_environment(cwd, updates)
                overrides.clear()
                rebuild_runtime()
                env = _effective_session_environment(cwd, overrides)
                print("provider config persisted to .cocoa/config.env")
                print_provider_status()
                continue
            if mode == "clear":
                _persist_environment(cwd, {"COCOA_PROVIDER": ""})
                overrides.clear()
                rebuild_runtime()
                env = _effective_session_environment(cwd, overrides)
                print("provider override cleared in workspace config")
                continue
            print("unknown /configure mode. use openai | codex-http | clear")
            continue
        if line.startswith("/set "):
            _, _, raw = line.partition(" ")
            if "=" in raw:
                key, value = raw.split("=", 1)
                key = key.strip()
                value = value.strip()
            else:
                tokens = shlex.split(raw)
                if len(tokens) < 2:
                    print("usage: /set KEY VALUE")
                    continue
                key = tokens[0]
                value = tokens[1]
            if not key:
                print("missing variable name")
                continue
            set_and_reload_env(key, value)
            env = _effective_session_environment(cwd, overrides)
            print(f"{key} set")
            print_provider_status()
            continue
        if line == "/status":
            print(f"thread: {thread.id}")
            print(f"cwd: {cwd}")
            print(f"log: {store.thread_path(thread.id)}")
            continue
        if line == "/provider":
            print(f"provider: {_resolve_provider_status_for_env(env)}")
            continue
        if line == "/model":
            print(f"model: {_resolve_provider_model_for_env(env)}")
            continue
        if line.startswith("/inspect"):
            _, _, raw_path = line.partition(" ")
            print_inspect(cwd, raw_path or ".", max_entries=80)
            continue
        if line.startswith("/run "):
            command = line.removeprefix("/run ").strip()
            if not command:
                print("missing command")
                continue
            result = await runtime.run_shell_turn(thread, command, shell)
            if result.stdout:
                print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
            if result.stderr:
                print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")
            if result.exit_code is not None:
                print(f"exit_code: {result.exit_code}")
            continue
        if line.startswith("/"):
            print("unknown command. type /help for commands.")
            continue

        message = await runtime.run_user_turn(thread, line)
        print(message)

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
