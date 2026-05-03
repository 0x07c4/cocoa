# Cost-Aware Agent Runtime

## Positioning

`cocoa` should not compete as another full coding-agent product.

Its short-term value is to act as a cost-aware, provider-agnostic agent runtime
for human-controlled vibe coding:

- route work across subscription, API, and local models
- keep provider output behind one event and approval protocol
- make expensive model calls intentional instead of habitual
- reuse workspace context instead of repeatedly paying to rediscover it
- preserve a replayable trail of proposals, approvals, and side effects

In this positioning, OpenCode, Codex CLI, Claude Code, DeepSeek, and local model
runners can all be access layers or providers. They should not define cocoa's
runtime semantics.

## Runtime Thesis

The runtime is the transaction-like layer between user intent and workspace side
effects.

The model can propose, but cocoa owns:

- turn and item identity
- context selection
- routing decisions
- proposal parsing
- approval state
- command and file-write boundaries
- event recording
- replay and projection

This keeps the product loop stable even when the provider changes.

## Short-Term User Problem

The immediate personal problem is token and quota anxiety during vibe coding.

Using one premium model for everything wastes expensive context on exploration,
summarization, and low-risk drafts. Cocoa should make the default loop more
controlled:

1. Codex plans when the task has meaningful design or risk
2. DeepSeek or a local model explores and drafts within the selected scope
3. cocoa records the result as a plan, proposal, diff, or reviewable item
4. the user escalates promising draft work to Codex for review, not automatic
   implementation takeover
5. expensive model sees the plan, selected files, diff, and constraints instead
   of a fresh full workspace dump
6. all file and command side effects still pass through cocoa approval

## Provider Mix

Target provider categories:

- ChatGPT Plus / Codex HTTP for high-value planning, architecture judgment, and
  review
- DeepSeek API for cheap exploration, drafting, bulk rewrite attempts, and
  alternative plans
- local OpenAI-compatible models for privacy-sensitive, offline, low-risk, or
  repetitive tasks
- OpenAI-compatible endpoints as the common adapter shape wherever possible

## Model Roles

Instead of configuring only a single model, cocoa should support roles:

```text
architect  -> Codex / premium reasoning model
reviewer   -> Codex / premium reasoning model
implementer -> DeepSeek or another cheap capable coding model
summarizer -> local or cheap model
private    -> local model
draft      -> DeepSeek or local model
```

Roles are product semantics. Providers and model names are configuration.

## User Modes

Initial modes can be coarse:

```text
/mode cheap       # prefer DeepSeek or local models
/mode balanced    # Codex plan -> cheap draft -> Codex review
/mode premium     # use ChatGPT/Codex for planning and review
/mode local       # only local providers
/usage            # show thread-level model and cost ledger
/escalate last    # send the previous item to a stronger role
```

The first implemented mode switch uses `COCOA_MODEL_MODE`. Mode-specific
profiles can be configured with `COCOA_CHEAP_*`, `COCOA_PREMIUM_*`,
`COCOA_LOCAL_*`, and `COCOA_BALANCED_*` variables. These override the standard
provider variables for the active mode, while falling back to the default
provider when no profile is configured.

The important workflow is `/escalate last`: cocoa should let DeepSeek or local
model output become structured input for Codex planning or review without
rebuilding context from scratch. Escalation should not imply that Codex takes
over implementation.

## Routing Policy

The first routing policy can be explicit and simple:

| Task | Preferred role |
| --- | --- |
| explain code | `summarizer` or `implementer` |
| summarize repository | `summarizer` |
| generate options | `architect` or `implementer` |
| draft patch | `implementer` or `draft` |
| final patch readiness | `reviewer` |
| review diff | `reviewer` |
| architecture judgment | `reviewer` |
| private content | `private` |

Automatic routing can come later. The first version should optimize for
visibility and user control.

## Usage Ledger

Each provider call should eventually record:

- thread id and turn id
- selected role
- provider and model
- routing reason
- input tokens
- output tokens
- estimated cost
- whether the call was an escalation

This is not only billing UI. It is a debugging surface for understanding why a
task became expensive.

## Context Reuse

Cost control depends more on context discipline than on cheap models alone.

Priority mechanisms:

- workspace file map cache
- explicit `@path` reads
- event replay from append-only JSONL
- thread summary
- turn-level plan item reuse
- diff-level context
- large-file truncation or relevant-slice extraction

The goal is that each model call receives bounded, intentional context.

## Implementation Order

### Phase 1: Documented Positioning and Profiles

- document cost-aware runtime positioning
- keep CLI as a debug projection, not the main product thesis
- define provider profile and model role shapes

### Phase 2: Modes and Ledger

- add `/mode cheap|balanced|premium|local` [done]
- record selected provider and model per turn [done]
- add `/usage` [done]
- preserve routing decisions as events [done]
- add cost tables for configured providers [pending]

### Phase 3: Escalation and Context Reuse

- add `/escalate last` [done]
- pass prior item, selected files, diff, and constraints to Codex for plan
  refinement or review [done]
- avoid full workspace re-scan for escalation calls [done]
- explicit-mode behavior: uses active provider, prints hint when not `premium` [done]

### Phase 4: Portfolio Packaging

- record a short demo: Codex plan, DeepSeek draft, Codex review, preview diff,
  approve, replay
- write the project as an agent runtime / developer tools artifact
- keep the claim focused on runtime protocol, cost routing, approval, and replay

## Non-Goals

Short term, cocoa should not chase:

- full TUI parity with OpenCode
- LSP integration
- plugin marketplace
- desktop app
- autonomous multi-agent orchestration
- cloud sync
- complex hidden auto-routing

These are product-surface multipliers. Cocoa first needs a clear runtime and
cost-control loop.
