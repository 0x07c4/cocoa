# AI Handoff

This file is the active shared artifact for multi-tool vibe coding before
`cocoa` has first-class plan / handoff / review items.

Use it when Codex runs in one window and OpenCode + DeepSeek runs in another.
Codex writes the architecture plan, task list, review findings, and acceptance
criteria. DeepSeek implements only the concrete tasks assigned here. The user
still approves the final side effect.

## Status

`ready-for-approval`

Allowed values:

- `planned`
- `implementing`
- `needs-review`
- `needs-fix`
- `ready-for-approval`

## Goal

Build `cocoa` by using `/home/chikee/workspace/claude-code-run` as the closest
local reference implementation, while keeping `cocoa` smaller and centered on
its own runtime contract:

- terminal-native, not a full Claude Code clone
- provider-agnostic, not provider-shaped
- cost-aware, not premium-model-for-everything
- human-in-the-loop, not autonomous by default
- event/proposal/approval driven, not hidden tool execution

## Reference Source

Use these `claude-code-run` areas as reference material:

- `src/QueryEngine.ts`: conversation lifecycle owner around the lower-level
  query loop
- `src/query.ts`: streaming loop, budget checks, tool orchestration, recovery
- `src/context.ts`: bounded user/system context assembly, git status, memory
  files, current date
- `src/Tool.ts` and `src/tools.ts`: tool interface, registry, permission-aware
  filtering
- `src/utils/tasks.ts` plus `TaskCreate/Get/List/UpdateTool`: durable task list
  shape and agent-visible task state
- `src/keybindings/`: explicit terminal keybinding schema and resolver
- `src/memdir/`: memory discovery as an optional context source, not product
  state
- `src/query/tokenBudget.ts` and compact services: budget/compaction concepts
- `spec/feature_*/spec-plan*.md`: concrete task-list format suitable for
  DeepSeek execution

Do not copy these parts into the first `cocoa` build:

- full React/Ink UI
- bundled 50+ tool surface
- remote-control server
- ACP bridge
- swarm/team orchestration
- browser/computer/voice use
- feature-flag sprawl
- autonomous background agents

## Target Shape For Cocoa

`cocoa` should borrow the architectural separation, not the product surface:

1. `AgentRuntime` remains the event/proposal/approval owner.
2. A new session/query layer should own one conversation lifecycle and call
   `AgentRuntime` instead of pushing more behavior into `cli.py`.
3. Tools should be explicit runtime capabilities with permission metadata, but
   provider-originated side effects still become proposals.
4. Context loading should be bounded, inspectable, and recorded as runtime
   items.
5. Tasks, handoffs, reviews, and plans should become first-class items or
   projections so Codex and DeepSeek do not rely on hidden conversation state.
6. Token/cost pressure should be visible through mode, usage, and budget
   records before any automatic routing is added.

## Implementation Phases

### Phase 1: First-Class Task / Handoff Items

Objective: replace the single mutable Markdown handoff with event-backed task
and handoff records that can still project to readable Markdown.

DeepSeek should implement this phase first.

Expected behavior:

- `cocoa` can create a task with subject, description, status, owner, blocked
  dependencies, and metadata.
- tasks are persisted in the existing append-only thread log, not a separate
  database
- `/tasks` lists current tasks for the thread
- `/task <id>` shows task details
- `/task-update <id> --status ...` records an update event
- handoff/review/plan content can be stored as item kinds, then rendered by
  `/show`

### Phase 2: Session Engine Boundary

Objective: extract a `SessionEngine` layer inspired by `QueryEngine.ts`.

Expected behavior:

- `cli.py` becomes a projection/controller, not the lifecycle owner
- one session engine owns thread id, provider, store, routing overrides, and
  per-turn context
- `ask`, `repl`, `/escalate`, `/run`, proposal apply/reject use the same engine
  methods
- runtime event semantics remain unchanged

### Phase 3: Context Builder

Objective: add a bounded context builder inspired by `context.ts`.

Expected behavior:

- include current date
- include bounded git status if the workspace is a git repo
- discover and read `AGENTS.md`, `CLAUDE.md`, and explicitly referenced
  instruction files within workspace scope
- keep `@path` references visible and recorded as `FILE_READ` or
  `WORKSPACE_INSPECT`
- avoid broad scans and truncate large content with clear markers

### Phase 4: Tool Registry And Permission Policy

Objective: introduce a minimal `Tool` interface without turning `cocoa` into a
large autonomous tool runner.

Initial core tools:

- workspace inspect
- file read
- shell command proposal
- file write/edit proposal
- task create/get/list/update

Constraints:

- model output may request tool-like actions, but side effects still become
  pending proposals unless the user explicitly accepts/applies
- read-only tools can run only inside workspace scope
- write/command tools must pass the existing approval boundary
- no MCP, web, browser, computer-use, or remote tools in this phase

### Phase 5: Budget And Compaction

Objective: make cost/context pressure visible before adding smarter automation.

Expected behavior:

- `/usage` keeps working
- add a simple budget warning when estimated context exceeds configured limits
- add a thread-summary item type
- `/compact` creates a summary item and records what was summarized
- escalation should prefer summary + selected items + diff over full replay

## DeepSeek Task List

### Task 0: Baseline Orientation

Status: `pending`

Scope:

- read `README.md`
- read `docs/architecture.md`
- read this `docs/ai-handoff.md`
- inspect `src/cocoa/models.py`, `runtime.py`, `projection.py`, `cli.py`
- do not modify unrelated files

Acceptance:

- report current task-relevant structure before editing
- identify any blocker before implementation

### Task 1: Add Task Item Types And Projection

Status: `completed`

Files:

- `src/cocoa/models.py`
- `src/cocoa/projection.py`
- `tests/test_projection.py`
- `tests/test_models.py` if needed

Implementation:

- add item kinds for `TASK`, `HANDOFF`, and `REVIEW`
- keep existing enum values backward compatible
- ensure projection keeps latest task state by item id
- add tests for task/handoff/review item projection

Acceptance:

- old thread logs still load
- new item kinds show through `load_thread_view`
- no change to command/file proposal behavior

### Task 2: Runtime Task CRUD Events

Status: `completed`

Files:

- `src/cocoa/runtime.py`
- `tests/test_runtime.py`

Implementation:

- add runtime methods to create, update, and list task items within a thread
- use append-only events only
- task content shape:
  - `subject`
  - `description`
  - `status`: `pending | in_progress | completed`
  - `owner`
  - `blocks`
  - `blocked_by`
  - `metadata`
- support metadata merge on update
- reject invalid status values

Acceptance:

- creating a task records an item event
- updating a task records a new latest item state
- listing tasks is projection-derived
- no separate task database is introduced

### Task 3: REPL Task Commands

Status: `completed`

Files:

- `src/cocoa/cli.py`
- `tests/test_cli.py`

Implementation:

- add `/tasks`
- add `/task <id>`
- add `/task-add <subject> -- <description>`
- add `/task-update <id> --status <status>`
- add completions for the new slash commands where current completion helpers
  already support command candidates

Acceptance:

- task commands work in the same thread as normal turns
- `/show <task_item_id>` displays the task item
- command parsing remains stdlib-only
- no prompt-toolkit-only behavior

### Task 4: Context Builder

Status: `completed`

Files:

- `src/cocoa/context.py` or a small runtime-local helper
- `src/cocoa/runtime.py`
- `tests/test_runtime.py`
- `tests/test_workspace.py` if needed

Implementation:

- move context assembly out of ad hoc runtime helpers
- include current date
- include bounded git status snapshot
- include workspace instruction files only when inside scope
- preserve and test existing `@path` behavior
- keep truncation limits explicit constants

Acceptance:

- provider request receives deterministic bounded context
- context reads are represented as runtime items
- ignored and out-of-workspace paths stay rejected

### Task 5: Minimal Tool Registry

Status: `completed`

Files:

- `src/cocoa/tools.py`
- `src/cocoa/runtime.py`
- `tests/test_runtime.py`
- `tests/test_tools.py` if useful

Implementation:

- define a minimal tool descriptor with name, description, read-only flag, and
  side-effect flag
- register the current shell and file proposal operations through descriptors
- do not expose autonomous tool execution to providers
- keep approval behavior unchanged

Acceptance:

- shell command proposals still require `/accept`
- file proposals still require `/apply`
- read-only operations remain workspace-scoped

### Task 6: Usage Budget Warning

Status: `completed`

Files:

- `src/cocoa/runtime.py`
- `src/cocoa/cli.py`
- `src/cocoa/config.py`
- `tests/test_config.py`
- `tests/test_runtime.py`

Implementation:

- add optional `budget.max_context_chars` and `budget.max_output_tokens`
  structured config keys
- record a warning in routing or usage payload when estimated context exceeds
  the configured limit
- show the warning in `/usage` or `/status`
- do not auto-compact in this task

Acceptance:

- default behavior unchanged when no budget is configured
- warning is event-backed and testable

## Constraints

- Keep Python-first and stdlib-first.
- Do not add pip dependencies.
- Do not change provider APIs unless a task explicitly requires it.
- Do not introduce React/Ink/TUI.
- Do not copy `claude-code-run` wholesale.
- Do not implement autonomous multi-agent orchestration.
- Do not bypass proposal/diff/apply gates.
- Keep DeepSeek work scoped to one task at a time.

## Implementer Prompt

Read this file and implement only the first `pending` DeepSeek task unless the
user explicitly asks for another task.

Before editing, report:

- which task you selected
- files you expect to change
- any blocker or ambiguity

After implementation, report:

- files changed
- summary of changes
- tests run
- known risks or blockers

If the plan is underspecified or appears wrong, stop and report the blocker
instead of inventing a larger design.

## Codex Review Checklist

Codex should review DeepSeek changes against these points:

- does the change preserve append-only runtime state?
- are all side effects still behind approval?
- does projection, not CLI ad hoc state, define current task state?
- are tests focused on the new contract?
- did DeepSeek avoid unrelated refactors?
- did the implementation copy only the useful pattern from `claude-code-run`?
