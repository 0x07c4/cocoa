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
PYTHONPATH=src python -m cocoa repl  # explicitly start repl

# Or just enter an interactive 会话 directly:
PYTHONPATH=src python -m cocoa
```

会话内可直接配置真实 provider（免重复导出环境变量）：

```sh
PYTHONPATH=src python -m cocoa repl

```

在 REPL 中直接输入：
- `/configure openai <api_key> <model> [base_url]`
- `/configure codex-http [model]`
- `/set [--persist|-p] KEY VALUE`（支持 `KEY=VALUE`，带 `--persist` 同时写入 `.cocoa/config.env`）
- `/persist`（保存当前会话内所有临时变量到 `.cocoa/config.env`）
- `/history`（查看当前 thread 的 turn projection）
- `/show <id|last>`（查看 turn 或 item projection）

`/configure` 会把配置落盘到当前工作区的 `.cocoa/config.env`，后续启动会自动读取。
REPL 输入区会显示当前 provider/thread 的 compact prompt。安装 `cocoa-agent[ui]`
后会自动使用 `prompt_toolkit` 的 styled session；否则降级为 stdlib 输入框。
在支持 GNU readline 的终端里，fallback 输入框仍支持 Tab 补全 slash commands、`/show`
的 turn/item id，以及 `/inspect` 的工作区路径。

`/provider` 会显示当前 provider，`/model` 会显示模型（未就绪时显示 `unknown`）。

Note:
- If a valid codex auth token is available in COCOA_CODEX_API_KEY/CODEX auth file, cocoa will auto-select `codex-http` on first start.

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
export COCOA_CODEX_API_KEY="..."            # OAuth/API token, optional if available in auth.json
export COCOA_CODEX_MODEL="..."              # optional, auto-discovered if omitted
export COCOA_CODEX_BASE_URL="https://chatgpt.com/backend-api/codex"  # optional
export COCOA_CODEX_TIMEOUT_SECONDS="60"     # optional
```

Optional Codex CLI bridge mapping:

- `COCOA_CODEX_HOME` maps to `CODEX_HOME` so you can point to a custom auth/session home.
- `COCOA_CODEX_BINARY` changes the executable path.
- `COCOA_CODEX_SANDBOX` supports `read-only`, `workspace-write`, `danger-full-access`.
- `COCOA_CODEX_APPROVAL` supports `untrusted`, `on-failure`, `on-request`, `never`.

Optional Codex HTTP env:

- `COCOA_CODEX_API_KEY` (optional, if not set auto reads `auth.json`)
- `COCOA_CODEX_HOME` (optional, default `~/.codex`) to locate `auth.json`
- `CODEX_API_KEY` (legacy CLI-compatible fallback)
- `COCOA_CODEX_BASE_URL` (defaults to `https://chatgpt.com/backend-api/codex`)
- `COCOA_CODEX_TEMPERATURE`
- `COCOA_CODEX_MAX_TOKENS`

`cocoa` also auto-discovers a valid token from `${COCOA_CODEX_HOME:-$CODEX_HOME:-~/.codex}/auth.json` when `COCOA_CODEX_API_KEY` is not set. `auth.json` must contain:

```json
{ "tokens": { "access_token": "..." } }
```

### Zero-config first try

If you already logged into the ChatGPT/Codex browser session locally, run:

```sh
export COCOA_PROVIDER="codex-http"
PYTHONPATH=src python -m cocoa doctor
PYTHONPATH=src python -m cocoa ask "what can you do?"
```

If `doctor` prints `provider: codex-http:<model>`, token discovery succeeded and runtime is ready.

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
