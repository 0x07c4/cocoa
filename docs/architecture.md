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

## Reference Implementation

`/home/chikee/workspace/claude-code-run` is the local reference for how a mature
terminal coding agent separates responsibilities. `cocoa` should borrow its
structural lessons, not its full product surface.

Useful reference areas:

- `QueryEngine.ts`: one conversation lifecycle owner above the lower-level query
  loop
- `query.ts`: budget checks, streaming recovery, and tool orchestration as
  runtime concerns
- `context.ts`: bounded context assembly for git status, instruction files, and
  current date
- `Tool.ts` / `tools.ts`: explicit tool descriptors, registry, and
  permission-aware filtering
- `utils/tasks.ts` and Task* tools: durable task state visible to agents and UI
- `spec/feature_*/spec-plan*.md`: implementation plans decomposed into tasks
  that a cheaper coding model can execute

The first `cocoa` build should not copy Claude Code's large Ink UI, remote
control bridge, ACP server, swarm/team orchestration, browser/computer/voice
tools, or broad feature-flag surface. Those are product multipliers; `cocoa`
still needs to make its runtime contract, approvals, context, task handoff, and
cost controls solid first.

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

## Context Builder Boundary

Workspace context assembly follows a clear two-layer boundary:

- **Context builder** (`src/cocoa/context.py`) owns workspace-aware content
  assembly: current date, git status, instruction files, workspace file map, and
  `@path` reference resolution. It returns a `BuiltContext` with assembled text
  and recorded item records. It does not write events, own thread state, or
  interact with providers.
- **Runtime** (`src/cocoa/runtime.py`) adapts `BuiltContext` into the runtime
  event stream. It calls `build_workspace_context()` from `context.py`, writes
  `ITEM_COMPLETED` events for each context item, and passes the assembled text
  to the provider request. It does not duplicate context assembly logic.

This boundary keeps context assembly testable without runtime setup and keeps
the runtime the single source of event writes and side-effect boundaries.

Current context builder scope:

- current date and time
- bounded `git status --short --branch` output with explicit truncation markers
- automatic discovery of root-level `AGENTS.md` and `CLAUDE.md` instruction
  files within workspace scope
- workspace file map via `WorkspaceScope.inspect()`, capped at 80 entries with
  clear cap markers
- explicit `@path` reference resolution in user prompts:
  - file references are recorded as `FILE_READ` items
  - directory references are recorded as `WORKSPACE_INSPECT` items
  - ignored paths and paths outside the workspace are rejected by
    `WorkspaceScope` and recorded as `FILE_READ` failed items
  - binary files are detected and recorded as `FILE_READ` failed items
  - missing paths are recorded as `FILE_READ` failed items
  - large files are truncated at 32 KB with truncation markers
  - reference count is capped at 6 with explicit skipped-reference markers

### Thread Context (Runtime-Owned)

Thread replay context — the textual summary of prior turns, commands, and file
operations visible to the model — remains owned by `AgentRuntime` and built
ad hoc in `_build_thread_context()`. Extracting it into `context.py` would
require either duplicating projection logic or pulling store access into the
context layer, which would weaken the runtime boundary. It stays in the runtime
for now.

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
- existing-file edits are represented as `FILE_WRITE` proposals with
  `operation=replace`, using exact `old` / `new` text replacement. The `old`
  text must match exactly once unless `replace_all=true`, which avoids fuzzy or
  hidden model-side patch application.
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

## Session Engine Boundary

The CLI remains a projection and controller. Long-lived conversation state
is owned by `SessionEngine` (`src/cocoa/session.py`), which is the layer
between the CLI and `AgentRuntime`:

- owns the active thread, provider, store, routing overrides, and per-turn
  context
- calls `AgentRuntime` for event creation and side-effect boundaries
- exposes methods for user turns, escalation, manual commands, proposals, and
  task operations
- keeps `cli.py` from accumulating lifecycle logic

This boundary does not weaken the append-only event model. The session layer
coordinates a turn; it does not become a second source of truth.

## Cost-Aware Routing Boundary

Provider selection is also runtime policy, not UI state and not provider state.
The long-term shape is:

- provider profiles describe access: endpoint, auth source, timeout, and default
  model
- model roles describe intent: architect, reviewer, implementer, summarizer,
  private, or draft
- routing decisions select a role, provider, and model for a turn
- usage records attach token and estimated-cost data to the turn

This keeps cost control tied to the same event trail as approvals and side
effects. A cheap model can draft or summarize, then a premium model can review
the prior item as structured context. The UI should project that decision, but
the append-only runtime log should remain the source of truth.

The first policy should favor explicit user modes over hidden automation:

- `cheap`: prefer inexpensive API or local models
- `balanced`: Codex plans, cheap models draft, Codex reviews
- `premium`: prefer ChatGPT/Codex-class models for planning and review
- `local`: keep all model calls local

See [cost-aware-runtime.md](cost-aware-runtime.md) for the product strategy and
implementation phases, and [vibe-coding-workflow.md](vibe-coding-workflow.md)
for the intended Codex + DeepSeek + local model collaboration loop.
