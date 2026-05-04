# Vibe Coding Workflow

## Thesis

`cocoa` should make model collaboration explicit instead of treating every
provider as an interchangeable chat box.

The target personal workflow is:

1. Codex plans.
2. DeepSeek or a local model drafts changes.
3. Codex reviews every model-generated diff.
4. The user approves the final side effect through `cocoa` / Solo.

This keeps ChatGPT Plus / Codex usage focused on high-value reasoning and
review instead of spending premium quota on routine exploration, typing, and
low-risk draft work.

## Model Roles

### Codex / ChatGPT Plus: Architect and Reviewer

Codex is the premium reasoning layer. It should normally stay in plan and
review mode.

Use Codex for:

- architecture decisions
- implementation plans
- risk assessment
- API and data-flow design
- review of DeepSeek-generated changes
- review of local-model-generated changes
- acceptance criteria
- final go / no-go judgment before user approval

Do not use Codex by default for:

- first-pass repository exploration
- repetitive code edits
- bulk rewrites
- routine test-fix loops
- directly applying patches

Codex may describe a required patch shape, but it should not be the default
patch producer. Its normal output should be a plan, review findings, or
acceptance criteria.

### DeepSeek V4: Implementer

DeepSeek is the main coding worker. It should do the cheap, high-volume work
that would otherwise burn premium quota.

Use DeepSeek for:

- codebase exploration after the user or runtime has scoped files
- implementation drafts from a Codex plan
- alternative implementation options
- boilerplate and mechanical rewrites
- first-pass tests
- fixing concrete review findings
- summarizing logs and failures for the next review pass

DeepSeek should not self-approve its own work. Its diffs move to Codex review
before they become eligible for user approval.

### Local Model: Assistant

The local model is an auxiliary layer, not the center of the workflow.

Use a local OpenAI-compatible endpoint for:

- privacy-sensitive summaries
- offline notes
- local context compression
- small local explanations
- naming or comment suggestions
- low-risk patch sketches

If the local model produces a code diff, that diff follows the same rule as a
DeepSeek diff: Codex must review it before the user applies it.

The runtime should stay runner-neutral. The current local setup can be LM Studio
with a `qwen3.5-9b` model exposed through an OpenAI-compatible endpoint, but
`cocoa` should not add Ollama-specific workflow assumptions or fallback logic.

## Default Loop

### 1. Plan With Codex

Codex receives the goal, selected context, constraints, and any known failure
state. It returns:

- the intended change
- files or areas to inspect
- implementation steps
- risk points
- tests and acceptance criteria

This plan should be structured enough that DeepSeek can execute it without
making major product or architecture decisions.

### 2. Draft With DeepSeek Or Local

DeepSeek is the default implementer. The local model can assist only for narrow
or private tasks.

The implementer receives the Codex plan plus scoped workspace context. It may
produce:

- a draft patch
- a `cocoa-proposal`
- a test plan
- failure summaries
- questions when the plan is underspecified

All proposed commands and file writes still enter `cocoa` as pending items.
They are not applied just because a provider produced them.

### 3. Review With Codex

Codex reviews the actual diff and any test output. Its review should lead with
findings, ordered by severity, and should answer:

- Does the change follow the plan?
- Does it preserve the intended runtime / approval boundary?
- Are there missing tests or obvious failure modes?
- Is the diff small enough and scoped correctly?
- Is the patch ready for user approval?

Codex review is mandatory for DeepSeek and local-model code changes.

### 4. Iterate With The Implementer

If Codex finds issues, DeepSeek or the local model fixes them. The corrected
diff returns to Codex review.

Codex should not silently take over implementation during this loop. It should
provide precise correction instructions and let the implementer produce the next
draft.

### 5. Approve With Cocoa / Solo

Codex approval means the patch is ready to present to the user. It does not mean
the patch is applied.

The final authority remains:

- preview in `cocoa` / Solo
- explicit user approval
- bounded command or file side effect
- recorded event trail

## Mode Mapping

| Mode | Primary use | Default provider role |
| --- | --- | --- |
| `cheap` | exploration, drafts, mechanical edits | DeepSeek implementer |
| `balanced` | normal workflow | Codex plan -> DeepSeek implement -> Codex review |
| `premium` | difficult design or review | Codex architect / reviewer only |
| `local` | private or offline auxiliary work | local assistant |

The important rule is that `premium` does not mean "let Codex write everything."
It means "spend premium reasoning on design and review."

## Escalation Semantics

`/escalate last` should mean:

- send the previous plan, draft, diff, or failure summary to Codex
- ask for plan refinement or review
- reuse selected files and prior runtime items instead of rescanning the whole
  workspace
- return findings or next-step instructions

It should not mean that Codex automatically takes over implementation.

## Manual Two-Window Handoff

Before `cocoa` has first-class plan / handoff / review items, use
[`ai-handoff.md`](ai-handoff.md) as the shared artifact between tools.

This is the temporary protocol for running Codex in one window and OpenCode +
DeepSeek in another:

1. Codex writes the goal, scope, plan, constraints, and acceptance criteria into
   `docs/ai-handoff.md`.
2. OpenCode + DeepSeek reads `@docs/ai-handoff.md` and implements only the
   implementer prompt.
3. DeepSeek updates implementation notes, test results, and sets status to
   `needs-review`.
4. Codex reviews the current diff against `@docs/ai-handoff.md` and writes
   review findings back into the file.
5. DeepSeek fixes only the Codex review findings.
6. Codex does a final review and marks the handoff as `ready-for-approval` when
   the diff can be shown to the user.

The shared artifact is deliberately plain Markdown. It avoids hidden tool state
and keeps both windows aligned even when the providers do not share a runtime
thread.

## Approval Boundary

Provider output is advisory. `cocoa` owns the side-effect boundary:

- commands require explicit acceptance
- file writes require diff preview and explicit apply
- ignored or out-of-workspace paths stay rejected by workspace scope
- review approval never bypasses user approval

This keeps the collaboration loop human-controlled even when multiple providers
are involved.

## Practical Defaults

- Start non-trivial tasks in `balanced`.
- Use Codex first when the task has architecture, product, migration, or safety
  implications.
- Use DeepSeek first when the task is exploratory, repetitive, or low risk.
- Use local mode only when privacy, offline work, or cheap summarization matters.
- Never apply DeepSeek or local model code without Codex review.
- Never treat Codex review as permission to skip `cocoa` / Solo approval.
