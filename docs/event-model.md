# Cocoa Event Model

This document defines the first Solo-facing event contract for Cocoa. It is a
minimal integration model, not a full replacement for Cocoa's current internal
runtime records.

## Why Append-Only Events

Cocoa uses append-only events because coding-agent work is a sequence of
decisions and side effects:

- the user creates an intent
- a model proposes a change
- Cocoa generates a previewable diff
- Solo or the user approves the proposal
- Cocoa applies the bounded side effect
- the run records the final result

Each step matters for review, replay, debugging, and UI projection. Updating a
single mutable task row would hide the sequence that made a file change safe.
Append-only JSONL keeps the runtime easy to write from Python and easy to read
from TypeScript without a database or server API.

The event log is the source of truth. Solo should derive timeline state from the
events and keep any UI state as a projection.

## Base Event Envelope

Each JSONL line is one UTF-8 JSON object. The base envelope is:

```json
{
  "schema_version": 1,
  "event_id": "evt_01JEXAMPLE0001",
  "type": "proposal.created",
  "created_at": "2026-05-04T10:00:03Z",
  "seq": 4,
  "task_id": "task_01JEXAMPLE",
  "run_id": "run_01JEXAMPLE",
  "actor": "model",
  "payload": {}
}
```

Fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `schema_version` | yes | Integer event schema version. Start with `1`. |
| `event_id` | yes | Globally unique event id. Consumers use it for idempotency. |
| `type` | yes | Dot-named event type. Unknown types must be ignored by old readers. |
| `created_at` | yes | ISO 8601 UTC timestamp. |
| `seq` | yes | Monotonic integer within one JSONL stream. File order is still authoritative. |
| `task_id` | yes | Stable id for the user-visible task. |
| `run_id` | no | Id for one execution attempt under the task. `task.created` may omit it. |
| `actor` | yes | One of `user`, `solo`, `cocoa`, or `model`. |
| `payload` | yes | Type-specific JSON object. Keep it plain JSON: strings, numbers, booleans, arrays, objects, and null. |

Readers should preserve unknown top-level fields and unknown payload fields when
possible. Writers should avoid embedding non-JSON values, Python reprs, binary
data, or language-specific types.

## Minimal File-Edit Flow

The first integration loop is proposal -> diff -> approval -> apply. These are
the minimal event types:

| Type | Actor | Purpose |
| --- | --- | --- |
| `task.created` | `user` or `solo` | Creates the user-visible task. |
| `run.created` | `cocoa` | Starts one Cocoa execution attempt for the task. |
| `model.message.created` | `model` | Records the model's natural-language response or summary. |
| `proposal.created` | `model` or `cocoa` | Records a pending side-effect proposal. |
| `diff.generated` | `cocoa` | Records a preview artifact for the proposal. |
| `approval.requested` | `cocoa` | Marks the proposal as waiting for user approval. |
| `approval.accepted` | `user` or `solo` | Records the explicit approval decision. |
| `apply.started` | `cocoa` | Starts the approved side effect. |
| `apply.completed` | `cocoa` | Records successful application of the side effect. |
| `run.completed` | `cocoa` | Closes the run. |

### Required Payload Shape

`task.created` payload:

```json
{
  "title": "Update README greeting",
  "intent": "Change the README greeting to mention Solo.",
  "cwd": "/workspace/project"
}
```

`run.created` payload:

```json
{
  "status": "running",
  "mode": "balanced",
  "provider": "codex-http",
  "model": "gpt-5.3-codex"
}
```

`model.message.created` payload:

```json
{
  "message_id": "msg_01JEXAMPLE",
  "role": "assistant",
  "content": "I will update README.md with a small greeting change."
}
```

`proposal.created` payload:

```json
{
  "proposal_id": "prop_01JEXAMPLE",
  "kind": "file_edit",
  "status": "pending",
  "path": "README.md",
  "reason": "Update the product greeting."
}
```

`diff.generated` payload:

```json
{
  "artifact_id": "art_01JEXAMPLE",
  "proposal_id": "prop_01JEXAMPLE",
  "kind": "unified_diff",
  "path": "README.md",
  "diff": "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-Hello\n+Hello from Solo\n"
}
```

`approval.requested` payload:

```json
{
  "approval_id": "appr_01JEXAMPLE",
  "proposal_id": "prop_01JEXAMPLE",
  "artifact_id": "art_01JEXAMPLE",
  "prompt": "Apply this file edit?"
}
```

`approval.accepted` payload:

```json
{
  "approval_id": "appr_01JEXAMPLE",
  "proposal_id": "prop_01JEXAMPLE",
  "accepted_by": "user"
}
```

`apply.started` payload:

```json
{
  "proposal_id": "prop_01JEXAMPLE",
  "approval_id": "appr_01JEXAMPLE",
  "artifact_id": "art_01JEXAMPLE",
  "path": "README.md",
  "operation": "file_edit"
}
```

`apply.completed` payload:

```json
{
  "proposal_id": "prop_01JEXAMPLE",
  "approval_id": "appr_01JEXAMPLE",
  "artifact_id": "art_01JEXAMPLE",
  "path": "README.md",
  "status": "completed"
}
```

`run.completed` payload:

```json
{
  "status": "completed",
  "summary": "Applied one README.md edit."
}
```

## Identity Relationships

`task_id` is the user-visible unit of work. Solo groups the timeline by this id.
A task may have multiple runs later, but the first model only needs one run.

`run_id` identifies one Cocoa execution attempt. Retry, regenerate, or resume can
create another run under the same task without overwriting the old events.

`event_id` identifies one append-only fact. It must never be reused. Solo should
deduplicate by `event_id` if it tails a file and reconnects.

`proposal_id` identifies one proposed side effect. The same `proposal_id` is used
by `proposal.created`, `diff.generated`, approval events, and apply events.

`approval_id` identifies one approval request and decision. A proposal can have
only one active approval in the first model. Later versions may allow repeated
approval requests after regeneration.

`artifact_id` identifies immutable preview material, such as a unified diff. The
artifact content should not change after `diff.generated`. If Cocoa regenerates a
diff, it should create a new `artifact_id`.

For the first file-edit loop:

```text
task_id
  run_id
    proposal_id
      artifact_id
      approval_id
      apply events
```

## Solo Consumption Rules

Solo should consume events as a projection:

1. Read JSONL line by line.
2. Ignore blank lines.
3. Parse each line as a JSON object.
4. Deduplicate by `event_id`.
5. Group by `task_id`, then by `run_id`.
6. Render events in file order, using `seq` as a sanity check.
7. Treat unknown event types as timeline entries with raw payload.
8. Treat unknown payload fields as additive metadata.

Suggested timeline projection:

- `task.created`: create the task row.
- `run.created`: show a running attempt.
- `model.message.created`: show model text.
- `proposal.created`: show a pending proposal card.
- `diff.generated`: attach the diff preview to the proposal card.
- `approval.requested`: show approve/reject controls.
- `approval.accepted`: mark the proposal as approved and disable decision controls.
- `apply.started`: show applying state.
- `apply.completed`: show applied state.
- `run.completed`: close the run and show the summary.

Solo should not infer that a proposal is applied just because it was approved.
The file change is complete only after `apply.completed`.

## Out Of Scope

The first event model intentionally does not define:

- a database schema
- an HTTP or WebSocket API
- multi-agent orchestration
- streaming token deltas
- command execution events
- rollback or undo semantics
- conflict resolution for stale diffs
- permission policy beyond accepted/rejected approval facts
- cross-machine synchronization
- binary artifacts or large artifact storage
- full schema migration machinery

Those can be added later as new event types or new payload fields without
breaking the first Solo timeline reader.
