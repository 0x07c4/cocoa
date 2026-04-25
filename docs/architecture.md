# cocoa Architecture

## Stack

- Main language: Python 3.12+
- Runtime dependencies: none in the MVP; stdlib-first by default
- Type discipline: typed Python, `py.typed`, and mypy-compatible boundaries
- Runtime protocol: dataclasses plus JSONL events
- CLI: stdlib `argparse`
- REPL: line-oriented stdlib input loop for the MVP
- Store: append-only JSONL under `.cocoa/threads`
- Tools: stdlib subprocess and filesystem APIs
- Future UI layer: `prompt_toolkit`, `rich`, or `Textual`
- Future native layer: C/C++ helpers for PTY, indexing, sandboxing, and diff

Native helpers are implementation details behind Python interfaces. They should
not own the runtime protocol, approval policy, provider protocol, or JSONL state.

## Runtime Primitives

`cocoa` uses the same broad shape as Codex's runtime model, but keeps the first
implementation small:

- `Thread`: long-lived workspace conversation
- `Turn`: one user-initiated interaction
- `Item`: a structured unit inside a turn
- `Event`: append-only record of lifecycle changes

The important rule is that UI messages, command cards, approvals, and future
file edits are projections of items. They should not become separate sources of
truth.

## Approval Boundary

The MVP asks for human approval before every shell command. Later policy can
become more granular:

- allow for one command
- allow for session
- deny for session
- require preview before apply
- restrict by workspace scope

## Provider Boundary

Providers return model responses or future response deltas. The runtime owns the
translation into `cocoa` items and events. Providers do not define the product
protocol.

Expected provider adapters:

- OpenAI-compatible Chat Completions HTTP
- Codex CLI bridge (`codex exec --json`) for compatibility
- Codex HTTP bridge (`/responses`) via `openai-codex` / `codex-http`
- local model runner
- external CLI bridge

The runtime should keep working if the provider changes.
