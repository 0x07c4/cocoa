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
