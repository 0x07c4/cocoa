from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

from . import __version__
from .providers import StubProvider
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
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Print local runtime status.")
    doctor.add_argument("--cwd", default=".", help="Workspace directory.")

    inspect = subparsers.add_parser("inspect", help="Inspect workspace files.")
    inspect.add_argument("path", nargs="?", default=".", help="Path inside workspace.")
    inspect.add_argument("--cwd", default=".", help="Workspace directory.")
    inspect.add_argument("--max", type=positive_int, default=120, help="Maximum entries.")

    ask = subparsers.add_parser("ask", help="Run one recorded user turn.")
    ask.add_argument("prompt", help="User prompt.")
    ask.add_argument("--cwd", default=".", help="Workspace directory.")

    repl = subparsers.add_parser("repl", help="Start a line-oriented cocoa session.")
    repl.add_argument("--cwd", default=".", help="Workspace directory.")

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


def resolve_cwd(raw: str) -> Path:
    cwd = Path(raw).expanduser().resolve()
    if not cwd.exists():
        raise SystemExit(f"workspace does not exist: {cwd}")
    if not cwd.is_dir():
        raise SystemExit(f"workspace is not a directory: {cwd}")
    return cwd


def make_runtime(cwd: Path) -> tuple[AgentRuntime, JsonlStore]:
    store = JsonlStore.for_workspace(cwd)
    return AgentRuntime(store=store, provider=StubProvider()), store


def print_doctor(cwd: Path) -> None:
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
    print("provider: stub")


def print_inspect(cwd: Path, path: str, max_entries: int) -> None:
    scope = WorkspaceScope(cwd)
    entries = scope.inspect(path, max_entries=max_entries)
    for entry in entries:
        print(f"{entry.path}\t{entry.size}")
    if len(entries) >= max_entries:
        print(f"... capped at {max_entries} entries")


async def run_ask(cwd: Path, prompt: str) -> None:
    runtime, store = make_runtime(cwd)
    thread = runtime.start_thread(cwd, title=prompt[:80])
    message = await runtime.run_user_turn(thread, prompt)
    print(message)
    print()
    print(f"thread: {thread.id}")
    print(f"log: {store.thread_path(thread.id)}")


async def run_repl(cwd: Path) -> None:
    runtime, store = make_runtime(cwd)
    thread = runtime.start_thread(cwd, title="repl")
    shell = ShellTool(ConsoleApprovalPrompter())

    print(f"cocoa {__version__}")
    print(f"thread: {thread.id}")
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
            print("/inspect [path]     list workspace files")
            print("/run <command>      run shell command after approval")
            print("/exit               quit")
            continue
        if line == "/status":
            print(f"thread: {thread.id}")
            print(f"cwd: {cwd}")
            print(f"log: {store.thread_path(thread.id)}")
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
    cwd = resolve_cwd(args.cwd)

    if args.command == "doctor":
        print_doctor(cwd)
        return 0
    if args.command == "inspect":
        print_inspect(cwd, args.path, args.max)
        return 0
    if args.command == "ask":
        asyncio.run(run_ask(cwd, args.prompt))
        return 0
    if args.command == "repl":
        asyncio.run(run_repl(cwd))
        return 0
    if args.command == "threads":
        print_threads(cwd)
        return 0

    raise SystemExit(f"unknown command: {args.command}")
