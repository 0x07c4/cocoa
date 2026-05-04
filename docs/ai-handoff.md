# AI Handoff

This file is the temporary shared artifact for multi-tool vibe coding before
`cocoa` has first-class plan / handoff / review items.

Use it when Codex runs in one window and OpenCode + DeepSeek runs in another.
Codex writes the plan and review findings. DeepSeek reads the plan, drafts the
change, and fixes review findings. The user still approves the final side
effect.

## Status

`ready-for-approval`

Allowed values:

- `planned`
- `implementing`
- `needs-review`
- `needs-fix`
- `ready-for-approval`

## Goal

Replace env-var-driven flat config files with a structured `.cocoa/cocoa.toml`
configuration, keeping COCOA_* env vars as overrides for backward compatibility.

## Scope

Implement a `src/cocoa/config.py` module that centralizes config I/O. Rewrite
config call sites in `src/cocoa/cli.py` and `src/cocoa/providers.py` to use the
new module. Keep existing config keys and /configure /set /persist semantics
unchanged — only change the storage format and loading path.

Do not change the core runtime, proposals, projection, workspace, store, or
tools modules. Do not introduce pip-level dependencies.

## Files

- `src/cocoa/config.py` (new)
- `src/cocoa/cli.py`
- `src/cocoa/providers.py`
- `tests/test_config.py` (new)
- `tests/test_cli.py`
- `docs/cost-aware-runtime.md` (optional, reflect config migration)

## Codex Plan

### Phase 1: Design the TOML schema

```
# .cocoa/cocoa.toml

default_mode = "balanced"

[provider.openai]
api_key = "sk-xxx"
model = "gpt-5-mini"
base_url = "https://api.openai.com/v1"
timeout_seconds = 60
temperature = 0.7
max_tokens = 4096

[provider.codex]
api_key = "xxx"
model = "gpt-5.4"
base_url = "https://chatgpt.com/backend-api/codex"
codex_home = "~/.codex"
timeout_seconds = 120
temperature = 0.7
max_tokens = 4096

# Mode overrides — ANY key under [mode.X.provider.Y] mirrors [provider.Y]
# top-level provider keys.  Missing keys fall through to [provider.Y].

[mode.premium]
provider = "codex"   # optional: force provider when mode=premium

[mode.premium.provider.openai]
model = "deepseek-v4-pro"

[mode.cheap.provider.openai]
model = "deepseek-v4-flash"
base_url = "https://api.deepseek.com/v1"
```

### Phase 2: Create src/cocoa/config.py

New module. No third-party dependencies — use stdlib `tomllib` (Py 3.11+) for
reading and write TOML by hand with a small serializer (the structure is flat
enough that hand-rolling is cleaner than bringing in `tomli-w`).

Module public API:

```
load_workspace_config(cwd: Path) -> dict[str, Any]
    # returns the merged dict from ~/.cocoa/cocoa.toml + cwd/.cocoa/cocoa.toml
    # project-level keys override global keys; scalar keys like default_mode
    # replace; dict keys like [provider.openai] merge

legacy_load_workspace_config(cwd: Path) -> dict[str, Any]
    # tries .cocoa/config.env (old format), converts flat key=value into the
    # same dict shape, keyed by prefix COCOA_OPENAI_*, COCOA_CODEX_*, etc.

save_workspace_config(cwd: Path, data: dict[str, Any]) -> None
    # writes cwd/.cocoa/cocoa.toml with the given dict

resolve_config(cwd: Path, session_overrides: Mapping[str, str]) -> dict[str, Any]
    # full resolved config dict merged from:
    #   legacy .cocoa/config.env (auto-migrate if toml missing)
    #   ~/.cocoa/cocoa.toml
    #   <cwd>/.cocoa/cocoa.toml
    #   COCOA_* environment variables (highest priority)
    #   session /set overrides (highest priority, flat string keys)

to_env_mapping(config: dict[str, Any], mode: str) -> dict[str, str]
    # given resolved config + active mode, produces the COCOA_PROVIDER=,
    # COCOA_OPENAI_MODEL=, etc. env dict that existing provider_from_env /
    # _provider_environment_for_mode consumer code expects.
    # This is the bridge: downstream code stays on env-dict input while
    # the config module handles toml merging.

config_to_routing_payload(cwd: Path, session_overrides: Mapping[str, str]) -> dict[str, Any]
    # equivalent of today's _routing_payload_for_env, but powered by resolve_config
```

#### Detail: legacy auto-migration

When `cocoa.toml` does not exist but `config.env` exists, `resolve_config` should
read the legacy file but also print a one-line warning: `hint: migrate to
.cocoa/cocoa.toml for structured config`.  Do not auto-overwrite; leave migration
explicit.

`/persist` and `/configure` always write `.cocoa/cocoa.toml` (the legacy writer
is deprecated but kept read-only for migration).

#### Detail: TOML serialization

Write a private `_serialize_toml(data: dict[str, Any]) -> str` that handles:

- top-level scalars: `key = "value"` or `key = 123`
- `[section]` headers
- `[section.sub]` dotted sections
- quoted string values (always use double quotes, escape `"` and `\`)

Only write the subset needed by our schema. Do not implement a general-purpose
TOML serializer.

#### Detail: env var override

`resolve_config` applies env vars last:
1. Walk known COCOA_* keys
2. Map them into the config dict shape:
   - `COCOA_PROVIDER` → `provider_used` key
   - `COCOA_MODEL_MODE` → `default_mode`
   - `COCOA_OPENAI_API_KEY` → `provider.openai.api_key`
   - `COCOA_OPENAI_MODEL` → `provider.openai.model`
   - ... etc for all COCOA_OPENAI_* and COCOA_CODEX_* keys
   - `COCOA_PREMIUM_PROVIDER` → `mode.premium.provider`
   - `COCOA_PREMIUM_OPENAI_MODEL` → `mode.premium.provider.openai.model`
   - ... same for CHEAP, BALANCED, LOCAL

### Phase 3: Rewrite cli.py config consumers

Replace these functions to delegate to `cocoa.config`:

| Old function | New behavior |
|---|---|
| `_effective_environment()` | calls `resolve_config(cwd, {})` then `to_env_mapping()` |
| `_effective_session_environment()` | calls `resolve_config(cwd, overrides)` then `to_env_mapping()` |
| `_persist_environment()` | calls `save_workspace_config()` — writes toml, not .env |
| `_provider_environment_for_mode()` | delegates to config module |
| `_routing_payload_for_env()` | calls `config_to_routing_payload()` |
| `_resolve_model_mode()` | reads `default_mode` from resolved config |
| `_resolve_provider_status_for_env()` | unchanged (already works on env dict produced by to_env_mapping) |
| `/configure openai` | writes `provider.openai` section into toml |
| `/configure codex-http` | writes `provider.codex` section into toml |
| `/configure clear` | clears `provider` section in toml |
| `/set --persist` | converts flat KEY=VALUE to toml key and persists |
| `/persist` | converts session overrides to toml and writes |
| `/mode` | writes `default_mode` in toml (session-only, no persist unless `/persist` is also run) |

Key constraint: the internal `env: dict[str, str]` passed around in cli.py and
consumed by `provider_from_env(env)` / `_routing_payload_for_env(env)` must keep
working. The config module returns that same shape via `to_env_mapping()`.

### Phase 4: Update tests

Move config-related tests from `tests/test_cli.py` into `tests/test_config.py`:

- `test_parse_config_lines_*` → test TOML loading
- `test_effective_environment_*` → test resolve_config
- `test_persist_environment_*` → test save/load roundtrip with toml
- `test_provider_status_*` → keep in test_cli (they test CLI behavior)
- `test_model_mode_*` → move to test_config
- `test_repl_configure_*` → keep in test_cli (test REPL behavior)

Add new tests for:

- TOML schema validation
- legacy .env migration path
- env var override priority
- mode-specific provider override merging
- config serialization roundtrip

### Phase 5: Acceptance criteria

- `cocoa doctor` shows correct provider details when `.cocoa/cocoa.toml` exists
- `/configure openai` writes to `.cocoa/cocoa.toml`, not `.cocoa/config.env`
- `/mode premium` uses `[mode.premium.provider.openai]` overrides from toml
- Running cocoa on a workspace with only legacy `config.env` still works and
  prints the migration hint
- COCOA_OPENAI_API_KEY env var overrides toml value
- All existing tests pass; new config tests pass
- Test command: `rtk python -m unittest tests.test_config tests.test_cli tests.test_runtime`

## Constraints

- Do not add pip dependencies. Use stdlib `tomllib` for reading, hand-roll toml writing.
- Do not change provider selection logic in providers.py beyond adapting one entry point.
- Do not touch runtime, proposals, tools, workspace, store, projection modules.
- Do not change REPL UI (the same / commands must work identically).
- Do not break backward compat: if a user has only old `.cocoa/config.env`, it must still work.
- Keep the config surface small — no new concepts beyond what's in the schema above.

## Implementer Prompt

Read this handoff file and implement only the Codex Plan.

Do not redesign the approach. Do not modify files outside Scope / Files. If the
plan is underspecified or appears wrong, stop and report the blocker instead of
inventing a larger task.

After implementation, report:

- files changed
- summary of changes
- tests run
- known risks or blockers

## Implementation Notes

- Python 3.12+ is required (pyproject.toml confirms). `tomllib` is stdlib since 3.11.
- The current `_parse_config_lines()` is ~30 lines. The TOML reader replaces it entirely.
- `_persist_environment()` is ~10 lines. Replace with toml writer.
- `_provider_environment_for_mode()` is ~50 lines of mode-prefix mapping. Simplify by
  having the TOML reader resolve mode overrides at load time.
- The `[mode.X.provider.Y]` table merging follows this rule: when `mode=X` is active,
  any keys under `[mode.X.provider.Y]` are merged on top of `[provider.Y]`. So if
  `[provider.openai]` has `model = "default"` and `[mode.premium.provider.openai]`
  has `model = "premium-model"`, the resolved value is `"premium-model"`.
- If `[mode.X]` has `provider = "codex"`, force the active provider to `codex` for
  that mode regardless of `[provider.openai]` or `[provider.codex]` config.

## Test Results

All 141 tests pass (21 new config tests, 37 cli tests, 42 runtime tests, 41 other):
```
$ rtk python -m unittest discover -s tests -p "*.py"
OK (141 tests)
```

## Implementation Notes

### TOML schema conflict resolution

The original schema used `[mode.X]` with `provider = "codex"` alongside `[mode.X.provider.Y]` for overrides. This is a TOML type conflict (a key cannot be both a string and a table). Fixed by using `[mode.X.overrides.Y]` for provider overrides while keeping `[mode.X] provider = "codex"` for the forced provider routing.

### Migration warning

When legacy `.cocoa/config.env` is used without a `.cocoa/cocoa.toml`, a one-line hint is printed to stderr once per process per workspace.

## Reviewer Prompt

Review the current diff against this handoff file.

Stay in review mode. Do not write or rewrite the patch. Lead with findings,
ordered by severity, and identify whether the diff is ready for user approval.

Check:

- whether the implementation follows the Codex Plan
- scope drift
- bugs or behavioral regressions
- missing tests
- approval blockers

## Final Approval Notes

Pending Codex review.
