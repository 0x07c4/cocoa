# cocoa

`cocoa` is a terminal-native agentic coding system.

The first implementation is Python-first:

- the package is typed Python and ships a `py.typed` marker
- the runtime owns `Thread -> Turn -> Item -> Event`
- tools run through explicit approval gates
- providers are adapters, not the product protocol
- local state is append-only JSONL for easy replay and debugging

Rust or C/C++ can be added later for native helpers such as PTY handling,
filesystem indexing, sandboxing, or high-volume diff work. Native helpers stay
behind Python interfaces; they should not own the runtime protocol, approval
policy, provider boundary, or state format.

## Run From Source

```sh
PYTHONPATH=src python -m cocoa doctor
PYTHONPATH=src python -m cocoa inspect .
PYTHONPATH=src python -m cocoa ask "summarize this workspace"
PYTHONPATH=src python -m cocoa repl
```

State is written under `.cocoa/` in the selected workspace.

## Provider

Without provider configuration, `cocoa` uses a deterministic stub provider.

To use an OpenAI-compatible Chat Completions endpoint, select it explicitly:

```sh
export COCOA_PROVIDER="openai"
export COCOA_OPENAI_API_KEY="..."
export COCOA_OPENAI_MODEL="..."
export COCOA_OPENAI_BASE_URL="https://api.openai.com/v1" # optional
PYTHONPATH=src python -m cocoa ask "summarize this workspace"
PYTHONPATH=src python -m cocoa ask "continue there" --thread <thread_id>

# Resume from existing thread in interactive mode:
PYTHONPATH=src python -m cocoa repl --thread <thread_id>
```

Supported optional variables:

- `COCOA_OPENAI_TIMEOUT_SECONDS`
- `COCOA_OPENAI_TEMPERATURE`
- `COCOA_OPENAI_MAX_TOKENS`

To use Codex (ChatGPT Plus/Pro account session):

```sh
# Legacy Codex CLI bridge (kept for compatibility)
export COCOA_PROVIDER="codex"
export COCOA_CODEX_BINARY="codex"           # optional
export COCOA_CODEX_MODEL="o3-mini"          # optional
export COCOA_CODEX_SANDBOX="read-only"      # optional
export COCOA_CODEX_APPROVAL="never"         # optional
export COCOA_CODEX_TIMEOUT_SECONDS="60"     # optional

# Recommended Codex HTTP mode (no local CLI process)
export COCOA_PROVIDER="codex-http"          # or codex-responses / openai-codex
export COCOA_CODEX_API_KEY="..."            # OAuth/API token
export COCOA_CODEX_MODEL="gpt-5-codex"     # required
export COCOA_CODEX_BASE_URL="https://chatgpt.com/backend-api/codex"  # optional
export COCOA_CODEX_TIMEOUT_SECONDS="60"     # optional
```

Optional Codex CLI bridge mapping:

- `COCOA_CODEX_HOME` maps to `CODEX_HOME` so you can point to a custom auth/session home.
- `COCOA_CODEX_BINARY` changes the executable path.
- `COCOA_CODEX_SANDBOX` supports `read-only`, `workspace-write`, `danger-full-access`.
- `COCOA_CODEX_APPROVAL` supports `untrusted`, `on-failure`, `on-request`, `never`.

Optional Codex HTTP env:

- `COCOA_CODEX_API_KEY` (recommended)
- `COCOA_CODEX_HOME` (optional, default `~/.codex`) to locate `auth.json`
- `CODEX_API_KEY` (legacy CLI-compatible fallback)
- `COCOA_CODEX_BASE_URL` (defaults to `https://chatgpt.com/backend-api/codex`)
- `COCOA_CODEX_TEMPERATURE`
- `COCOA_CODEX_MAX_TOKENS`

`cocoa` also auto-discovers a valid token from `${COCOA_CODEX_HOME:-$CODEX_HOME:-~/.codex}/auth.json` when `COCOA_CODEX_API_KEY` is not set. `auth.json` must contain:

```json
{ "tokens": { "access_token": "..." } }
```

`OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_BASE_URL` are accepted as
fallbacks when `COCOA_PROVIDER=openai`. The provider only returns model text;
`cocoa` runtime still owns item creation, event recording, approval state, and
persistence.

## Current Scope

This is an MVP skeleton. It intentionally starts without a model dependency.
The bundled provider is a stub, and an OpenAI-compatible Chat Completions
adapter is available through environment variables. The next real step is
streaming provider deltas and structured proposal items while keeping the
runtime protocol stable.

The current implementation records a `Thread`, starts `Turn`s, creates `Item`s,
and stores lifecycle `Event`s. Events are the append-only trail, not a child
level under items.

## Design Line

`cocoa` should not be a free-form autonomous agent loop. The core interaction
model is targeted to become:

1. user intent
2. agent proposal
3. preview
4. explicit approval
5. bounded side effect
6. recorded event trail

The MVP only implements the recorded event trail and a shell approval gate. File
preview/apply and agent-generated proposals are next-layer work.

The CLI is only one projection of the runtime. Future TUI/editor clients should
read the same event stream rather than inventing their own state model.
