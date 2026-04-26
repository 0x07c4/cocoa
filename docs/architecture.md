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

## Projection Boundary

The append-only JSONL event stream is the source of truth. Read-side clients
should not parse ad hoc event rows directly. They should consume a projection
that reconstructs:

- thread metadata
- ordered turns
- latest item state per item id
- turn-scoped errors

The first CLI projection commands are `/history` and `/show <id|last>`. Future
TUI/editor surfaces should read the same projection layer instead of inventing a
separate session state.

## Workspace Context

Every provider request may include a compact workspace context owned by the
runtime, not by the provider. The first implementation includes a lightweight
file map for orientation and supports explicit `@path` references in the user
prompt:

- file references are recorded as `FILE_READ` items
- directory references are recorded as `WORKSPACE_INSPECT` items
- ignored paths and paths outside the workspace are rejected by `WorkspaceScope`
- large files are truncated before entering model context

This keeps the product loop explicit: the user decides which files matter, cocoa
records what was read, and the provider receives enough context to propose real
workspace changes.

## Approval Boundary

The MVP asks for human approval before every shell command. Later policy can
become more granular:

- allow for one command
- allow for session
- deny for session
- require preview before apply
- restrict by workspace scope

Provider-originated side-effect suggestions use the same approval boundary. A
model may emit a `cocoa-proposal` JSON block, but the runtime only records it as
pending items with `approval=requested`:

- `COMMAND` proposals require `/accept <item_id>` plus the shell approval gate.
- `FILE_WRITE` proposals include a unified diff preview and require
  `/apply <item_id>`. Writes are constrained by `WorkspaceScope`, so ignored
  paths and paths outside the workspace are rejected.
- pending proposals can be redisplayed with `/pending`, file diffs can be
  redisplayed with `/diff <item_id>`, and either command or file proposals can be
  explicitly closed with `/reject <item_id>`.

## Provider Boundary

Providers return model responses or future response deltas. The runtime owns the
translation into `cocoa` items and events. Providers do not define the product
protocol.

Expected provider adapters:

- OpenAI-compatible Chat Completions HTTP
- Codex CLI bridge (`codex exec --json`) for compatibility
- Codex HTTP bridge (`/responses`) via `openai-codex` / `codex-http`, with auto-discovery of tokens from `CODEX_HOME`/`COCOA_CODEX_HOME` `auth.json` when no explicit key is supplied.
- Codex HTTP model selection prefers `COCOA_CODEX_MODEL`/`OPENAI_MODEL`; when absent it loads `.../models?client_version=1.0.0` and falls back to a safe default list.
- local model runner
- external CLI bridge

The runtime should keep working if the provider changes.
