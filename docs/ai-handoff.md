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

Status: `implemented (Tasks 7-10)`

Objective: extract a `SessionEngine` layer inspired by `QueryEngine.ts`.

Expected behavior:

- `cli.py` becomes a projection/controller, not the lifecycle owner
- one session engine owns thread id, provider, store, routing overrides, and
  per-turn context
- `ask`, `repl`, `/escalate`, `/run`, proposal apply/reject use the same engine
  methods
- runtime event semantics remain unchanged

### Phase 3: Context Builder

Status: `implemented (Tasks 11-13)`

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

Status: `partially implemented (Tasks 14-16)`

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

Status: `completed`

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

### Task 7: Add SessionEngine Wrapper

Status: `completed`

Files:

- `src/cocoa/session.py` (new)
- `tests/test_session.py` (new)

Implementation:

- add a small `SessionEngine` class that owns:
  - `runtime: AgentRuntime`
  - `store: JsonlStore`
  - `thread: ThreadRecord`
  - `cwd: Path`
  - `overrides` dict for session env overrides
  - internal `ShellTool(ConsoleApprovalPrompter())`
- constructors or helpers for:
  - starting a new session with a title
  - resuming an existing thread id
  - set/clear session overrides (rebuilds runtime)
  - env/mode/provider-status introspection
- expose thin methods that delegate to the existing runtime:
  - `run_user_turn(prompt)` — one-shot
  - `run_user_turn_with_result(prompt)` — returns `UserTurnResult`
  - `run_escalation_turn()` — delegates to runtime with built-in routing
  - `run_shell_turn(command)` — uses internal shell
  - `run_proposed_command(item_id)` — accept proposal
  - `apply_proposed_file_write(item_id)` — apply file write
  - `reject_pending_item(item_id)` — reject proposal
  - `create_task(...)`
  - `update_task(...)`
  - `list_tasks()`

Acceptance:

- no behavior change in `AgentRuntime`
- no CLI behavior change yet
- tests prove a session can start, resume, run a user turn, create/list/update
  tasks, and delegate proposal apply/reject through the existing runtime
- thread id and log path remain the same as before

### Task 8: Route Ask Through SessionEngine

Status: `completed`

Files:

- `src/cocoa/cli.py`
- `tests/test_cli.py`
- `tests/test_session.py` if needed

Implementation:

- update `run_ask()` to create/resume a `SessionEngine` instead of directly
  juggling `runtime`, `store`, and `thread`
- keep output exactly the same:
  - context items
  - model message
  - proposals
  - thread id
  - log path
- leave REPL unchanged in this task

Acceptance:

- existing `ask` behavior and tests still pass
- `run_ask()` no longer calls `runtime.start_thread()` or
  `runtime.resume_thread()` directly
- no provider/config/routing behavior changes

### Task 9: Route REPL Runtime Actions Through SessionEngine

Status: `completed`

Files:

- `src/cocoa/cli.py`
- `tests/test_cli.py`
- `tests/test_session.py` if needed

Implementation:

- update REPL runtime actions to call `SessionEngine` methods:
  - normal prompt turn
  - `/escalate`
  - `/run`
  - `/accept`
  - `/apply`
  - `/reject`
  - `/tasks`
  - `/task-add`
  - `/task-update`
- keep projection-only commands reading from the same store:
  - `/history`
  - `/show`
  - `/pending`
  - `/diff`
  - `/usage`
- keep provider reload behavior explicit: when `/mode`, `/set`, or
  `/configure` rebuilds provider/runtime, rebuild the session wrapper around
  the same thread id instead of creating a new thread.

Acceptance:

- existing REPL tests still pass
- provider reconfiguration keeps the current thread
- no direct REPL calls to `runtime.run_user_turn_with_result`,
  `runtime.run_escalation_turn`, `runtime.run_shell_turn`,
  `runtime.run_proposed_command`, `runtime.apply_proposed_file_write`,
  `runtime.reject_pending_item`, `runtime.create_task`,
  `runtime.update_task`, or `runtime.list_tasks`
- approval and file apply behavior remains unchanged

### Task 10: Remove CLI Lifecycle Ownership

Status: `completed`

Files:

- `src/cocoa/cli.py`
- `src/cocoa/session.py`
- `docs/architecture.md`
- `tests/test_cli.py`
- `tests/test_session.py`

Implementation:

- reduce `_resolve_runtime_from_env()` / `make_runtime()` usage or rename them
  so they produce a `SessionEngine` where appropriate
- keep CLI responsible for parsing, printing, completion, and config commands
- keep `SessionEngine` responsible for active thread lifecycle and runtime
  delegation
- update architecture docs to mark the initial SessionEngine boundary as done

Acceptance:

- `cli.py` no longer owns active thread lifecycle outside session construction
- `AgentRuntime` remains the event/proposal/approval owner
- `SessionEngine` does not introduce a second source of truth
- `mypy src/cocoa` passes
- `PYTHONPATH=src python -m unittest discover -s tests -q` passes

### Task 11: Harden Context Bounds And Truncation Markers

Status: `completed`

Files:

- `src/cocoa/context.py`
- `tests/test_runtime.py`
- `tests/test_workspace.py` if needed

Implementation:

- keep the existing `build_workspace_context()` shape; do not rewrite the
  context system
- make all bounded context surfaces explicit and testable:
  - git status output has a max line/char bound and a clear truncation marker
  - workspace map states when it hit the entry cap
  - instruction files and `@path` file reads include a clear truncation marker
    in both item content and provider context text
- keep broad scans bounded by existing workspace scope and max-entry limits
- do not include ignored paths, `.git`, `.cocoa`, dependency/build folders, or
  out-of-workspace paths in context

Acceptance:

- provider request receives bounded context when git status, workspace map, or
  file content is large
- truncation is visible to the model and visible in recorded context items
- existing context behavior remains compatible for small workspaces
- `PYTHONPATH=src python -m unittest discover -s tests -q` passes

### Task 12: Strengthen Instruction File And `@path` Scope Tests

Status: `completed`

Files:

- `src/cocoa/context.py`
- `tests/test_runtime.py`
- `tests/test_workspace.py` if needed

Implementation:

- preserve automatic root-level `AGENTS.md` and `CLAUDE.md` discovery
- treat nested or extra instruction files as explicit context only when the
  prompt references them with `@path`
- add tests for:
  - `CLAUDE.md` inclusion
  - ignored `@path` rejection
  - out-of-workspace `@path` rejection
  - missing `@path` recorded as a failed context item
  - binary `@path` recorded as a failed context item
  - directory `@path` recorded as `WORKSPACE_INSPECT`
  - reference-count cap includes a skipped-reference marker

Acceptance:

- every accepted `@path` reference is visible in provider context and recorded
  as `FILE_READ` or `WORKSPACE_INSPECT`
- every rejected `@path` reference is recorded as a failed context item with a
  useful error
- instruction files never bypass workspace scope or ignore rules
- `PYTHONPATH=src python -m unittest discover -s tests -q` passes

### Task 13: Clarify Runtime/Context Boundary

Status: `completed`

Files:

- `src/cocoa/context.py`
- `src/cocoa/runtime.py`
- `docs/architecture.md`
- `tests/test_runtime.py`

Implementation:

- keep `AgentRuntime` as the event owner and provider request owner
- keep workspace context assembly in `src/cocoa/context.py`; `runtime.py` should
  only adapt `BuiltContext` into runtime events/items
- do not move proposal, approval, task, or thread replay semantics into
  `context.py`
- document the boundary in `docs/architecture.md`
- if thread replay extraction is attempted, keep it small and behavior-neutral;
  otherwise document why it remains runtime-owned for now

Acceptance:

- context builder owns workspace/date/git/instruction/`@path` context assembly
- runtime remains the only source of event writes and side-effect boundaries
- no provider prompt regression in existing runtime tests
- `mypy src/cocoa` passes
- `PYTHONPATH=src python -m unittest discover -s tests -q` passes

### Task 14: Formalize Tool Registry Metadata

Status: `completed`

Files:

- `src/cocoa/tools.py`
- `src/cocoa/runtime.py`
- `src/cocoa/session.py`
- `tests/test_tools.py`
- `tests/test_session.py` if needed

Implementation:

- keep the existing static builtin registry; do not add dynamic plugins, MCP,
  browser, web, or remote tools
- extend the minimal tool metadata so each builtin tool states:
  - stable name
  - description
  - read-only vs side-effect behavior
  - workspace scope requirement where relevant
  - approval/proposal requirement where relevant
- make the initial core set complete:
  - `workspace_inspect`
  - `file_read`
  - `shell_command`
  - `file_write`
  - `file_edit` if file edit remains a distinct proposal capability
  - `task_create`
  - `task_get`
  - `task_list`
  - `task_update`
- keep compatibility for existing `list_registered_tools()` callers

Acceptance:

- registry tests prove names are stable and unique
- side-effect tools are never read-only
- write/command tools require approval/proposal metadata
- read-only workspace tools declare workspace scope
- `mypy src/cocoa` passes
- `PYTHONPATH=src python -m unittest discover -s tests -q` passes

### Task 15: Add A Testable Permission Policy Layer

Status: `completed`

What was added:

- `Permission` enum in `tools.py` with `ALLOW`, `REJECT`, `REQUIRES_APPROVAL`
- `PermissionResult` frozen dataclass in `tools.py` with `decision` + `reason`
- `ToolPermissionPolicy` class in `tools.py` that checks tool calls against:
  - unknown tool name → REJECT
  - side-effect or requires-approval tool → REQUIRES_APPROVAL
  - workspace-scoped tool without scope → REJECT
  - workspace-scoped tool with out-of-scope path → REJECT
  - workspace-scoped tool with ignored path → REJECT
  - workspace-scoped tool with valid/missing in-scope path → ALLOW
  - read-only non-workspace tool → ALLOW
- 17 tests in `test_tools.py` covering all acceptance criteria
- Policy is data-only: instantiates from `ToolDescriptor` tuples, never
  executes tools, never touches filesystem during evaluation
- Existing proposal/apply gates unchanged

Read references first:

- `/home/chikee/workspace/codex/codex-rs/core/src/config/permissions.rs`
- `/home/chikee/workspace/codex/codex-rs/core/src/tools/handlers/mod.rs`
- `/home/chikee/workspace/codex/codex-rs/core/src/tools/handlers/shell.rs`
- `/home/chikee/workspace/codex/codex-rs/protocol/src/request_permissions.rs`
- `/home/chikee/workspace/claude-code-run/src/Tool.ts`
- `/home/chikee/workspace/claude-code-run/src/utils/permissions/permissions.ts`
- `/home/chikee/workspace/claude-code-run/src/tools.ts`

### Task 16: Surface Tool Contract To Providers Without Auto-Execution

Status: `pending`

Files:

- `src/cocoa/providers.py`
- `src/cocoa/runtime.py` if needed
- `tests/test_providers.py`
- `tests/test_runtime.py` if needed
- `docs/architecture.md`

Implementation:

- expose the builtin tool contract in provider instructions or request context
  so the model understands available capabilities and permission boundaries
- preserve the existing `cocoa-proposal` path for command and file write/edit
  side effects
- explicitly tell providers that `cocoa` will not auto-run side-effect tools;
  they must produce pending proposals for user approval
- do not implement a model-driven autonomous tool loop in this task
- document the tool registry / permission policy boundary in architecture docs

Acceptance:

- provider tests prove the prompt/instructions include the tool contract and
  side-effect approval rule
- existing proposal parsing tests still pass
- runtime still records read context as items and side effects as pending
  proposals or approved apply/run events
- no MCP/web/browser/computer-use tool surface is introduced
- `mypy src/cocoa` passes
- `PYTHONPATH=src python -m unittest discover -s tests -q` passes

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
