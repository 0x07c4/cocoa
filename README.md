# cocoa

`cocoa` is a terminal-native agentic coding system.

Its current direction is a cost-aware, provider-agnostic agent runtime for
human-controlled vibe coding. It is not trying to become another full OpenCode
or Codex CLI clone. The CLI is the first projection of a runtime that owns
events, approvals, replay, provider routing, and workspace side-effect
boundaries.

The first implementation is Python-first:

- the package is typed Python and ships a `py.typed` marker
- the runtime owns `Thread -> Turn -> Item -> Event`
- tools run through explicit approval gates
- providers are adapters, not the product protocol
- local state is append-only JSONL for easy replay and debugging

See [docs/cost-aware-runtime.md](docs/cost-aware-runtime.md) for the short-term
runtime strategy around provider routing, usage tracking, and escalation. See
[docs/vibe-coding-workflow.md](docs/vibe-coding-workflow.md) for the intended
ChatGPT Plus/Codex + DeepSeek + local model collaboration loop.

C/C++ can be added later for native helpers such as PTY handling,
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
- `/help`（显示命令列表）
- `/status`（查看会话状态：thread、mode、provider、model）
- `/mode [cheap|balanced|premium|local]`（查看或切换当前模型路由模式）
- `/configure openai <api_key> <model> [base_url]`
- `/configure codex-http [model]`
- `/usage`（查看当前 thread 的 provider/model/token 使用记录）
- `/pending`（重新列出当前 thread 里未处理的 proposals）
- `/diff <item_id>`（重新查看 pending file write 的 diff）
- `/escalate last`（提交上一轮到 reviewer）
- `/accept <item_id>`（执行 provider 提出的 pending command proposal）
- `/apply <item_id>`（应用 provider 提出的 pending file write proposal）
- `/reject <item_id>`（拒绝 pending command/file write proposal）
- `/exit` 或 `/quit`（退出 REPL）

`/configure` 会把配置落盘到当前工作区的 `.cocoa/config.env`，后续启动会自动读取。
每一轮会自动附带一个轻量 workspace file map；输入里出现 `@path` 时，cocoa 会在
调用 provider 前读取对应工作区文件或目录，并记录为 `FILE_READ` /
`WORKSPACE_INSPECT` item。例如：

```text
解释 @src/cocoa/cli.py 里的 REPL 输入流程
改一下 @README.md，补一段真实体验说明
```

REPL 输入区会显示当前 provider/thread 的 compact prompt。安装 `cocoa-agent[ui]`
后会自动使用 `prompt_toolkit` 的 styled session；否则降级为 stdlib 输入框。
在真实 TTY 里，fallback 输入框也会在输入 `/` 时直接显示 slash command 候选，
不需要先按 Tab；上下键可移动选中项，Tab/Enter 可接受候选。
输入行支持左右移动、Home/End、Delete、Ctrl+A/E、Ctrl+K、Ctrl+W 这些基础编辑键。
普通输入里的 `@path` 和 command 参数同样会出现在候选里。
非 TTY 或不支持 raw terminal 的环境会
回退到普通 `input()`。

Provider 可以在普通文本后附一个 `cocoa-proposal` JSON block 来提出命令、精确文件编辑或文件写入建议。
`cocoa` 会把建议记录为 pending item。`/pending` 可以随时重新列出未处理建议，
`/diff <item_id>` 可以重新查看文件改动，`/reject <item_id>` 会把建议明确记录为
rejected。命令只在用户执行 `/accept <item_id>` 并通过确认后才运行；文件写入会先
展示 unified diff，只在用户执行 `/apply <item_id>` 后写入。对已有文件应优先使用
`edits` 的 `old` / `new` 精确替换；`old` 默认必须唯一匹配，否则 cocoa 会拒绝应用：

````markdown
```cocoa-proposal
{
  "commands": [
    {"command": "python -m unittest discover -s tests -q", "reason": "verify changes"}
  ],
  "edits": [
    {
      "path": "README.md",
      "old": "old exact text\n",
      "new": "new replacement text\n",
      "reason": "update existing documentation"
    }
  ],
  "write_files": [
    {"path": "hello.txt", "content": "hello\n", "reason": "create demo file"}
  ]
}
```
````

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

### Cost-aware modes

`COCOA_MODEL_MODE` selects the active routing mode. Supported values are
`balanced`, `cheap`, `premium`, and `local`; the default is `balanced`.

Mode-specific variables can override the normal provider configuration:

```sh
COCOA_MODEL_MODE=cheap
COCOA_CHEAP_PROVIDER=openai
COCOA_CHEAP_OPENAI_API_KEY=...
COCOA_CHEAP_OPENAI_MODEL=deepseek-v4
COCOA_CHEAP_OPENAI_BASE_URL=https://api.deepseek.com/v1

COCOA_PREMIUM_PROVIDER=codex-http
COCOA_PREMIUM_CODEX_MODEL=gpt-5.3-codex

COCOA_LOCAL_PROVIDER=openai
COCOA_LOCAL_OPENAI_API_KEY=local
COCOA_LOCAL_OPENAI_MODEL=qwen3.5-9b
COCOA_LOCAL_OPENAI_BASE_URL=http://127.0.0.1:1234/v1
```

The mode layer is intentionally thin in the first version. It selects a provider
profile, records a `routing_decision` event, and records a `usage_recorded`
event with provider/model plus actual or estimated token counts.

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
The bundled provider is a stub. OpenAI-compatible, Codex HTTP, and Codex CLI
adapters are available through environment/config variables. The current coding
loop supports workspace context, explicit `@path` file reads, structured command
proposals, exact file edit proposals, and file write proposals while keeping the
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

The MVP implements the recorded event trail, shell approval gate, command
proposal approval, workspace context injection, explicit file reads, exact file
edit preview/apply, and file write preview/apply. Richer patch formats,
streaming deltas, and rollback are next-layer work.

The CLI is only one projection of the runtime. Future TUI/editor clients should
read the same event stream rather than inventing their own state model.
