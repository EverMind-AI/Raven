# CLI Sub-Agent Dispatch — Design

**Date:** 2026-07-15
**Status:** Approved (brainstorming → spec)
**Repo:** AgentScope 2.0 working copy (RavenX_demo)

## 1. Overview

Add a **user-global registry of CLI sub-agents** to the AgentScope app service and
web UI. Each registered sub-agent is a shell command *template* containing a
`{prompt}` placeholder, e.g.:

- `claude -p {prompt} --dangerously-skip-permissions`
- `python /path/domain_agent.py --task {prompt}`
- `codex exec {prompt}`

Every registered sub-agent is surfaced to the unified (leader) agent as **one
tool** on its `Toolkit`, named and described by its config. The agent dispatches
work by calling that tool with a `prompt`; the tool substitutes the prompt into
the template and runs the external CLI, returning its stdout as the tool result.

This realizes RavenX integration path 2 ("wrap a CLI directly as a tool") from
`CLAUDE.md`, made user-configurable at runtime through a new right-sidebar
**Sub-Agents** panel backed by a new `/subagent/` REST resource.

## 2. Goals / Non-goals

**Goals**
- Register/list/update/delete CLI sub-agents per user, via REST and a web-UI panel.
- Each sub-agent becomes a callable tool on the leader agent's toolkit automatically.
- Support **arbitrary** CLI sub-agents (heterogeneous, different internal
  architectures) via a generic command template — not just Claude Code.
- Be **purely additive**: no behavior change to existing toolkit/chat/workspace
  execution paths.

**Non-goals (v1)**
- HTTP / A2A sub-agent invocation (the factory design makes this a later drop-in).
- Streaming incremental sub-agent output (output arrives as one chunk on
  completion, exactly like the built-in `Bash` tool).
- Cross-user sharing of sub-agents (Credentials support it; skipped here).
- Per-config human-in-the-loop (HITL) approval toggle.

## 3. Confirmed decisions

| Decision | Choice | Rationale |
|---|---|---|
| **Scope** | User-global (mirror Credentials) | Configure once, available in every session; purely additive backend (no changes to workspace/toolkit core). |
| **Invocation** | Generic command template with `{prompt}` | Supports any CLI-invoked domain sub-agent — matches the RavenX heterogeneous-sub-agent goal. |
| **Approval** | Auto-run (no HITL) | Frictionless autonomous dispatch. The sub-agent CLI configs are user-authored, so trust is explicit. |

Minor calls (flagged for reversal): `env` stored as a plain `dict[str,str]` like
MCP's `env` (not `SecretStr`); the add-form is hand-written (better command-
template UX) while the backend still exposes `/subagent/schemas` for future types.

## 4. Architecture

### 4.1 Why a `ToolBase`, not MCP

`claude -p "..."` (and peer CLIs) are **not** MCP stdio servers — they do not
speak MCP framing over stdio, so `StdioMCPConfig` / `MCPClient` would fail at
`connect()`. The correct vehicle is a `ToolBase` subclass that shells out,
modeled on the built-in `Bash` tool
(`src/agentscope/tool/_builtin/_bash.py`, `call()` at line ~670 →
`backend.exec_shell(...)`).

### 4.2 Attachment hook (no core changes)

`create_app(storage=..., workspace_manager=..., extra_agent_tools=...)`
(`src/agentscope/app/_app.py:43`) accepts a caller-supplied
`AgentToolFactory = async (user_id, agent_id, session_id) -> list[ToolBase]`
(`src/agentscope/app/_types.py:27`). It is invoked once per turn during toolkit
assembly and its result appended to the toolkit's `basic` group
(`src/agentscope/app/_service/_toolkit.py:235-240`). Because the reference
`examples/agent_service/main.py` constructs `storage` and `workspace_manager`
as locals before calling `create_app` (lines 41-67), a closure factory can
capture them and be passed as one extra kwarg — **no changes** to `Toolkit`,
`get_toolkit`, or `ChatService`.

### 4.3 Backend package `src/agentscope/subagent/` (new, mirrors `credential/`)

| File | Role | Mirror of |
|---|---|---|
| `_base.py` | `SubAgentConfigBase(BaseModel)` (has `type` discriminator) + `CliSubAgentConfig` (`type="cli_subagent"`) | `credential/_base.py` |
| `_factory.py` | `SubAgentFactory.from_dict / register / list_schemas` (discriminated-union `TypeAdapter` on `type`) | `credential/_factory.py` |
| `_tool.py` | `CliSubAgentTool(ToolBase)` | `tool/_builtin/_bash.py` |
| `_agent_tools.py` | `make_subagent_tool_factory(storage, workspace_manager) -> AgentToolFactory` | — |
| `__init__.py` | Public re-exports | `credential/__init__.py` |

**`CliSubAgentConfig` fields**

- `id: str` (generated) — record id.
- `type: Literal["cli_subagent"]` — discriminator.
- `name: str` — the tool name shown to the LLM; validated `^[A-Za-z0-9_-]+$`.
- `description: str` — **the dispatch signal**: when to use this sub-agent /
  what it is good at. Exposed to the LLM as the tool description.
- `command: str` — shell command template; **must contain `{prompt}`**.
- `cwd: str | None` — optional working directory.
- `env: dict[str, str] | None` — optional extra environment variables.
- `timeout: int` — seconds; default **600** (`claude -p` runs are slow).

**`CliSubAgentTool` behavior** (mirrors `Bash`)

- `input_schema`: a single required parameter `prompt: string`.
- On `call(prompt=...)`:
  1. `argv = shlex.split(self._command)`.
  2. For **each** argv token, substring-replace `{prompt}` with the raw prompt
     string. This keeps the prompt inside a **single argv token** (no
     `/bin/sh -c`, no shell interpolation → the prompt cannot inject shell), and
     handles both a standalone `{prompt}` token and embedded forms such as
     `--task={prompt}`.
  3. If `env` is set, prepend an `env` prefix so variables are applied without a
     shell: `argv = ["env", *(f"{k}={v}" for k, v in env.items()), *argv]`
     (`BackendBase.exec_shell` takes no `env` kwarg; the `env(1)` binary is the
     shell-free way to set them — assumes a POSIX `env`, which holds on the
     Linux target).
  4. `result = await self._backend.exec_shell(argv, cwd=self._cwd, timeout=self._timeout)`.
  5. Yield a terminal `ToolChunk`: stdout as a `TextBlock` truncated at 30 000
     chars on success (`state=RUNNING, is_last=True`); on non-zero exit or
     timeout, `state=ERROR` with stderr.
- `check_permissions` → `ALLOW` (auto-run).
- `is_read_only = False`, `is_concurrency_safe = False` (conservative, like
  `Bash` — two sub-agents can mutate the shared workspace and race).

**`make_subagent_tool_factory`**

Closure capturing `storage` and `workspace_manager`. On each turn:
1. `configs = await storage.list_subagents(user_id)`.
2. Resolve the session's workspace backend (same environment as the session's
   `Bash` tool); fall back to `LocalBackend()` if resolution fails.
3. Build one `CliSubAgentTool` per valid config; **skip + log** malformed ones
   (same posture as malformed model cards).
4. Return the list.

### 4.4 Storage (additions, mirror Credentials)

- `src/agentscope/app/storage/_model/_subagent.py`: `SubAgentRecord(_RecordBase)`
  with `user_id: str`, `data: dict` (mirror `_credential.py`). Registered in
  `_model/__init__.py`.
- `RedisStorage.KeyConfig`: add
  `subagent = "agentscope:user:{user_id}:subagent:{subagent_id}"` and
  `subagent_index = "agentscope:user:{user_id}:subagents"`.
- `StorageBase`: add abstract `upsert_subagent` / `list_subagents` /
  `get_subagent` / `delete_subagent` (copy the four credential signatures).
- `RedisStorage`: implement them (copy the credential block: `SET` record +
  `SADD` into the per-user index Set; list reads the index then GETs each;
  delete removes key + `SREM`).

### 4.5 REST (additions, mirror Credentials)

- `src/agentscope/app/_router/_schema/_subagent.py`: `CreateSubAgentRequest`
  (`data: dict`), `CreateSubAgentResponse` (`subagent_id: str`),
  `UpdateSubAgentRequest` (`data: dict`), `SubAgentView`,
  `ListSubAgentsResponse` (`subagents: list[SubAgentView]`, `total: int`).
  Registered in `_schema/__init__.py`.
- `src/agentscope/app/_router/_subagent.py`:
  `subagent_router = APIRouter(prefix="/subagent", tags=["subagent"])` with:
  - `GET /subagent/schemas` → `SubAgentFactory.list_schemas()`
  - `GET /subagent/` → list for `X-User-ID`
  - `POST /subagent/` (201) → `storage.upsert_subagent(user_id, SubAgentFactory.from_dict(body.data))`
  - `PATCH /subagent/{subagent_id}` → update
  - `DELETE /subagent/{subagent_id}` (204)
- Register the router in `app/_router/__init__.py` and in `app/_app.py`
  (import tuple + `include_router` loop).

### 4.6 Wiring (one added kwarg)

`examples/agent_service/main.py`:
```python
from agentscope.subagent import make_subagent_tool_factory
...
app = create_app(
    storage=storage,
    workspace_manager=...,
    extra_agent_tools=make_subagent_tool_factory(storage, workspace_manager),
    ...
)
```

### 4.7 Frontend — a "Sub-Agents" dock panel (mirrors the MCP panel)

The right sidebar is a multi-panel dock keyed by a `PanelKey` union with a
`Record<PanelKey, PanelDescriptor>` lookup — a missing key is a compile error
(the safety net).

| File | Action | Mirror of |
|---|---|---|
| `src/api/types.ts` | Add `SubAgentView`, `CreateSubAgentRequest`, `CreateSubAgentResponse`, `UpdateSubAgentRequest`, `SubAgentListResponse` | Credential/MCP type blocks |
| `src/api/subagent.ts` (new) | `subagentApi = { list, schemas, create, update, delete }` → `/subagent/` | `src/api/credential.ts` |
| `src/api/index.ts` | `export { subagentApi } from './subagent'` | — |
| `src/hooks/useSubagents.ts` (new) | list + `refetch()` after each mutation | `src/hooks/useCredentials.ts` |
| `src/components/panel/SubagentPanel.tsx` (new) | Presentational: search, list of `<Item>` rows with `<Trash>` + `DeleteDialog`, "Add" button | `src/components/panel/McpPanel.tsx` |
| `src/components/dialog/AddSubagentDialog.tsx` (new) | Hand-written form: name, description, command (with "`{prompt}` will be replaced with the task" hint), cwd, env, timeout | `src/components/dialog/AddSkillDialog.tsx` |
| `src/components/panel/PanelDock.tsx` | `PanelKey` += `'subagent'` | — |
| `src/pages/chat/ChatViewport.tsx` | Add `subagent:` descriptor to the `panels` memo (+ dep array), a `DropdownMenuCheckboxItem` toggle, and wire `useSubagents(...)` | MCP descriptor/toggle |
| `src/i18n/locales/en.json` + `zh.json` | `subagent` block (title, description, searchPlaceholder, add, empty*) + dialog keys | `mcp` block |

Refresh model: no React Query / websocket for config lists — the hook manually
`refetch()`es after a mutation; the running agent re-reads config server-side on
the next chat turn.

## 5. Data flow

1. User fills the **Add Sub-Agent** form → `POST /subagent/` with
   `{ data: { type: "cli_subagent", name, description, command, cwd?, env?, timeout? } }`
   → stored per `X-User-ID` in Redis.
2. On the **next chat turn**, `get_toolkit` calls
   `extra_agent_tools(user_id, agent_id, session_id)` → the factory reads the
   user's stored sub-agents → builds `CliSubAgentTool`s → appended to the
   toolkit's `basic` group.
3. The leader agent sees one tool per sub-agent; it calls e.g.
   `claude_code(prompt="…")` → the tool substitutes into
   `claude -p "<prompt>" --dangerously-skip-permissions` → runs via the backend
   → stdout returned as the tool result. Auto-run (no confirmation).

## 6. Error handling

- **Config validation:** `command` missing `{prompt}` or empty `name` → 422 at
  create/update; defensive re-check in the tool factory (skip + log).
- **Sub-Agent failure:** non-zero exit or timeout → `ToolChunk(state=ERROR)`
  carrying stderr (mirror `Bash`); the leader agent sees the failure and can
  react.
- **CLI not installed:** surfaced as an exec error → `ERROR` tool result.
- **Output size:** stdout truncated at 30 000 chars (mirror `Bash`).

## 7. Security

- The LLM-supplied prompt is passed as a **single argv token with no shell**, so
  it cannot inject shell metacharacters into the sub-agent invocation.
- The command *template* itself is user-authored configuration; the
  `--dangerously-skip-permissions` flag is the user's explicit choice for their
  own sub-agent CLIs.
- Sub-Agents run against the session's workspace backend (the same sandbox as
  the session's `Bash` tool), so sandboxing (Docker/E2B/etc.) applies when
  configured.

## 8. Testing (TDD, backend-first)

- `CliSubAgentTool`: with a fake `BackendBase`, assert argv substitution for a
  standalone `{prompt}` token **and** an embedded `--task={prompt}` form (each
  stays one token), the `env` prefix construction, success chunk content/state,
  error/timeout chunk, and the missing-`{prompt}` guard. Whole-structure asserts.
- `SubAgentFactory`: `from_dict` round-trip, `list_schemas` shape, unknown-type
  rejection.
- Storage: CRUD round-trip (`upsert` → `list` → `get` → `delete`), using the
  same test double/pattern as the credential storage tests; `AnyString` /
  `AnyValue` for ids/timestamps.
- Router: FastAPI `TestClient` create → list → delete round-trip with a
  `X-User-ID` header.
- `make_subagent_tool_factory`: builds N tools from N stored configs; skips
  malformed ones.
- Follow repo conventions: `tests/*_test.py`, whole-structure comparisons,
  `AnyString`/`AnyValue` from `tests/utils.py`.

## 9. Complete file change list

**Add (backend):** `src/agentscope/subagent/{__init__,_base,_factory,_tool,_agent_tools}.py`;
`src/agentscope/app/storage/_model/_subagent.py`;
`src/agentscope/app/_router/_subagent.py`;
`src/agentscope/app/_router/_schema/_subagent.py`;
`tests/subagent_test.py` (+ any split test files).

**Touch (backend, additions only):** `src/agentscope/app/storage/_base.py`;
`src/agentscope/app/storage/_redis_storage.py`;
`src/agentscope/app/storage/_model/__init__.py`;
`src/agentscope/app/_router/__init__.py`; `src/agentscope/app/_app.py`;
`src/agentscope/app/_router/_schema/__init__.py`;
`examples/agent_service/main.py`.

**Add (frontend):** `src/api/subagent.ts`; `src/hooks/useSubagents.ts`;
`src/components/panel/SubagentPanel.tsx`;
`src/components/dialog/AddSubagentDialog.tsx`.

**Touch (frontend):** `src/api/types.ts`; `src/api/index.ts`;
`src/components/panel/PanelDock.tsx`; `src/pages/chat/ChatViewport.tsx`;
`src/i18n/locales/en.json`; `src/i18n/locales/zh.json`.

**Do NOT touch:** `src/agentscope/tool/_toolkit.py`,
`src/agentscope/app/_service/_toolkit.py`, `src/agentscope/app/_service/_chat.py`
— the `extra_agent_tools` hook already plumbs custom tools through.
