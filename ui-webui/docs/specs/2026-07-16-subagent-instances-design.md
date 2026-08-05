# Stateful Sub-Agent Instances + Prototype Sidebar + Instance Monitor — Design

**Date:** 2026-07-16
**Status:** Approved (brainstorming → spec)
**Repo:** AgentScope 2.0 working copy (RavenX_demo)

## 1. Overview

Refactor how CLI sub-agents are used, so a single **prototype** (blueprint) can
be **instantiated** into multiple stateful **instances** whose conversational
context is preserved across calls.

1. **Prototype → instance split.** A prototype (the existing `CliSubAgentConfig`)
   gains an optional `resume_command`. When set, the prototype is **stateful**:
   the main agent creates named instances of it and re-invokes them, and the CLI
   keeps context between calls (e.g. Claude Code via `--session-id` then
   `--resume`). When `resume_command` is empty, the prototype is **stateless** —
   byte-for-byte today's behavior.

2. **Agent-driven instances.** The main agent sees **one tool per prototype**.
   For a stateful prototype the tool takes an `instance` handle: an unknown/absent
   handle → **CREATE** (RavenX generates a uuid = the CLI session id, runs
   `command`), a known handle → **RESUME** (runs `resume_command` with the stored
   uuid). One prototype can back many concurrent instances.

3. **Per-session instances.** The `handle → agent_id` registry is scoped to the
   current RavenX chat session and persisted in Redis. A new session starts empty
   (the agent re-creates on first call).

4. **Left sidebar for prototypes.** A new left sidebar is the single home for
   prototype CRUD (list + create + inline edit + delete), replacing the current
   right-dock panel + create modal.

5. **Right-dock instance monitor.** The right dock is repurposed into a
   read-only, per-session **Instance Monitor**: the list of instances spun up
   this session and, per instance, its interaction history (each delegated
   prompt → sub-agent output). It is derived from the session transcript — no new
   heavy storage, no new read endpoint.

6. **Transcript badge.** In the main chat transcript, a stateful sub-agent call
   shows a small `[handle · new]` / `[handle · resumed]` marker.

The whole change is **additive**: existing stored prototypes have no
`resume_command`, so they remain stateless with unchanged runtime and UI
behavior. No data migration.

## 2. Goals / Non-goals

**Goals**
- One new optional field on `CliSubAgentConfig` (`resume_command`) and a new
  `{agent_id}` placeholder, enabling stateful create-then-resume prototypes.
- Per-prototype tool that, when stateful, exposes an `instance` input and manages
  create-vs-resume against a per-session registry.
- Per-session `handle → agent_id` registry persisted in Redis, used **only** by
  the tool to decide create-vs-resume.
- A new left sidebar for full prototype CRUD (including inline edit and the
  `env` field, both currently missing), replacing the right-dock prototype panel
  and the create modal.
- A right-dock per-session Instance Monitor (instance list + per-instance
  interaction history) derived from the transcript.
- A `[handle · new/resumed]` badge on stateful sub-agent tool calls in the
  transcript.
- Full backward compatibility for existing stateless prototypes; no migration.

**Non-goals (v1)**
- A **"forget" / kill** action to reset an instance handle. The agent can already
  start fresh by choosing a new handle; adding forget would need a new
  `DELETE …/subagent-instances/{handle}` endpoint and mutable-registry semantics.
  Deferred. (Design note in §9 so it can be added cleanly later.)
- Persisting per-instance prompt/output **history** in Redis. The monitor derives
  history from the message history the frontend already holds, so we avoid
  duplicating (potentially large) outputs into storage.
- Cross-session or per-user instances (scope is per RavenX session).
- Changing the stateless execution path, the permission model
  (`check_permissions` still always ALLOWs), or the output truncation behavior.
- Non-Claude specifics: the templates are generic; Claude Code is just the first
  consumer.

## 3. Background — current state (verified)

- **Prototype config** — `CliSubAgentConfig` (`src/agentscope/subagent/_base.py`):
  `id`, `type="cli_subagent"`, `name` (regex `^[A-Za-z0-9_-]+$`), `description`,
  `command` (validated to contain `{prompt}` and to have a literal first token),
  `cwd`, `env`, `timeout=600`. Persisted **per-user** in Redis
  (`agentscope:user:{user_id}:subagent:{id}` + a set index) via the
  `SubAgentRecord` model and `storage.upsert/list/get/delete_subagent`.
- **Runtime tool** — `CliSubAgentTool` (`src/agentscope/subagent/_tool.py`):
  **stateless**. `input_schema` is `{prompt}` only. `_build_argv` `shlex.split`s
  `command`, substitutes `{prompt}` in each token, optionally prepends
  `env KEY=VAL`, and runs `backend.exec_shell(argv, cwd, timeout)` with no shell.
  Error strings use the noun "Sub-Agent"; timeout sentinel is
  `exit_code == -1 and stderr == b"timed out"`; output truncated at 30000 chars.
  `check_permissions` always ALLOWs. No identity/session persists across calls.
- **Wiring** — `make_subagent_tool_factory(storage, workspace_manager)`
  (`src/agentscope/subagent/_agent_tools.py`) is an `extra_agent_tools` factory
  (`AgentToolFactory = (user_id, agent_id, session_id) -> list[ToolBase]`). It is
  invoked **per chat turn** by `get_toolkit` (`_service/_toolkit.py:234-240`),
  reads `list_subagents(user_id)`, and builds one `CliSubAgentTool` per config,
  defaulting `cwd` to the resolved session workdir. Opt-in: the app author passes
  it into `create_app(extra_agent_tools=...)` (see `examples/agent_service/main.py`).
- **Router** — `src/agentscope/app/_router/_subagent.py`: `GET /subagent/schemas`,
  `GET /subagent/`, `POST /subagent/`, `PATCH /subagent/{id}`,
  `DELETE /subagent/{id}`. Request/response carry an untyped `data: dict` whose
  shape is `CliSubAgentConfig`. Schemas come from `SubAgentFactory.list_schemas()`.
- **Frontend** — right-dock `SubagentPanel` (list/search/delete) +
  `AddSubagentDialog` modal (create only; no edit; no `env` field), owned by
  `ChatViewport`, fed by `useSubagents` → `subagentApi` (`list/create/update/
  delete`; `update` exists but is **unused**). `subagentNameSet =
  new Set(subagents.map(s => s.data.name))` is provided via
  `SubagentNamesContext`; a tool call is a sub-agent iff its `call.name` is in
  that set. Transcript rendering: `renderToolCall` →
  `defaultRenderHeader(pair, t, isSubagent)` (label `tool.callSubagent`) and
  `subagentRenderBody` (prompt box + result). The layout convention for a new
  left sidebar is a self-contained `<Sidebar collapsible="none" className="…
  border-r">` inserted into the `ChatPageInner` flex row before `<ChatViewport>`
  (mirroring `TeamSidebar.tsx`).
- **Metadata plumbing (verified)** — `ToolResultBlock`
  (`src/agentscope/message/_block.py:183`) has `metadata: dict[str, Any]`.
  `ToolResponse`/chunk `metadata` merges into it (`tool/_response.py:143`,
  `message/_base.py:419`). The frontend consumes the same SDK `ToolResultBlock`
  as `pair.result` (`tool-renderers/types.ts`). So a tool can attach
  `{instance, agent_id, action}` to its result and the frontend reads it from
  `pair.result.metadata`.

## 4. Concepts

| | **Prototype** | **Instance** |
|---|---|---|
| Type | `CliSubAgentConfig` (persisted `SubAgentRecord`) | `SubAgentInstanceRecord` (per-session registry) |
| Identity | `id`, `name` | `handle` (agent-chosen) → `agent_id` (uuid = CLI session id) |
| Scope | Per-user (Redis, unchanged) | **Per RavenX session** (Redis, new) |
| Stateful? | Only if `resume_command` is set | Always |
| Created by | User, in the left sidebar | The main agent, at call time |

**Placeholders in command templates:** `{prompt}` (existing) and `{agent_id}`
(new). `{agent_id}` is the uuid RavenX generates when it CREATEs an instance and
reuses on RESUME; it is the CLI's own session identifier (e.g. Claude's
`--session-id` / `--resume` argument).

## 5. Backend design

### 5.1 `CliSubAgentConfig` (`subagent/_base.py`) — one new field

Add:
```python
resume_command: str | None = Field(
    default=None,
    description=(
        "Optional command template used to RESUME an existing instance. When "
        "set, the prototype is stateful: the first call to an instance handle "
        "runs `command` (create) and later calls run this template (resume). "
        "Both `command` and `resume_command` must then contain `{prompt}` and "
        "`{agent_id}`. When empty, the prototype is stateless (one-shot per "
        "call), identical to prior behavior."
    ),
)
```
`command`'s docstring is updated to note it is the create/first-call template and
may contain `{agent_id}`.

**Validators** (extend the existing `_require_prompt_placeholder` /
`_require_literal_executable`):
- `command` keeps requiring `{prompt}` and a literal first token (unchanged).
- New model-level validator: **if `resume_command` is non-empty**, then
  - both `command` and `resume_command` must contain `{prompt}` **and**
    `{agent_id}`, and
  - `resume_command`'s first `shlex.split` token must be a literal executable
    (same rule as `command`).
- If `resume_command` is empty/None: no `{agent_id}` requirement anywhere
  (stateless prototypes need not use it; if `command` happens to contain
  `{agent_id}`, a fresh uuid is substituted per call — harmless).

### 5.2 Instance registry (new) — per session

New record model `SubAgentInstanceRecord`
(`src/agentscope/app/storage/_model/_subagent_instance.py`):
```python
class SubAgentInstanceRecord(_RecordBase):
    session_id: str
    handle: str          # agent-chosen instance handle, unique within a session
    agent_id: str        # uuid = CLI session id, generated at CREATE
    prototype_name: str  # the CliSubAgentConfig.name this instance is of
```
(`_RecordBase` supplies `id`, `created_at`, `updated_at`.) Note: **no history**
field — history is derived on the frontend from the transcript (§6.2).

New `StorageBase` methods + `RedisStorage` implementation:
```python
async def get_subagent_instance(session_id: str, handle: str) -> SubAgentInstanceRecord | None
async def upsert_subagent_instance(record: SubAgentInstanceRecord) -> str
async def list_subagent_instances(session_id: str) -> list[SubAgentInstanceRecord]
```
Redis layout (mirrors the existing per-user subagent keys, but session-scoped):
- `agentscope:session:{session_id}:subagent_instance:{handle}` → record JSON
- `agentscope:session:{session_id}:subagent_instances` → Redis set of handles

TTL: use the same `_set_with_ttl` helper as other per-session/user records so
instances expire with the session. `list_subagent_instances` is included for
completeness/tests and any future forget UI; the monitor does **not** call it.

A tiny facade wraps these for the tool so it isn't coupled to `StorageBase`
directly and is trivially fakeable in tests:
```python
class SessionInstanceRegistry:
    def __init__(self, storage, session_id): ...
    async def resolve(self, handle, prototype_name) -> tuple[str, bool]:
        """Return (agent_id, created). If handle is unknown, generate a uuid,
        persist it, and return (uuid, True). Otherwise return (stored_id, False)."""
```
`resolve` is where the uuid is minted (`uuid.uuid4().hex`) and the record is
upserted on first sight.

### 5.3 `CliSubAgentTool` (`subagent/_tool.py`) — instance-aware

Constructor gains `resume_command: str | None = None` and
`registry: SessionInstanceRegistry | None = None`. Behavior:
- **`is_stateful`** = `bool(resume_command)` **and** `registry is not None`.
- **`input_schema`:**
  - stateless → `{prompt}` (unchanged).
  - stateful → `{prompt, instance}` with `instance` **required**. `instance`
    description: *"A stable handle for this sub-agent instance. Reuse the same
    handle to continue the same conversation (context is preserved); use a new
    handle to start a fresh instance."*
- **`call(prompt, instance=None)`:**
  - stateless: build argv from `command` (substitute `{prompt}`; if `{agent_id}`
    is present, substitute a fresh `uuid4().hex`), run once — as today.
  - stateful:
    1. `agent_id, created = await self._registry.resolve(instance, self._name)`.
    2. template = `command` if `created` else `resume_command`.
    3. `argv = self._build_argv(prompt, template, agent_id)`.
    4. run via `backend.exec_shell`; same output/timeout/error handling as today.
    5. attach `metadata = {"instance": instance, "agent_id": agent_id,
       "action": "create" if created else "resume"}` to the terminal
       `ToolResponse`/chunk.
- **`_build_argv(prompt, template, agent_id=None)`** generalized: `shlex.split`
  the chosen `template`, replace `{prompt}` and (if present) `{agent_id}` inside
  each token, keep the existing `env KEY=VAL` prefix logic.

Error handling for the registry (best-effort, non-fatal):
- If `resolve` raises before running (storage unreachable): log a warning and
  fall back to a **create** run with a fresh uuid (degraded continuity), so a
  single call still succeeds.
- If the post-CREATE persist inside `resolve` fails after minting the uuid: log;
  the call still returns output. A later call may re-CREATE (a duplicate
  instance) — acceptable and documented.

### 5.4 `make_subagent_tool_factory` (`subagent/_agent_tools.py`)

Pass the new inputs through when building each tool:
```python
CliSubAgentTool(
    name=config.name,
    description=config.description,
    command=config.command,
    resume_command=config.resume_command,
    cwd=config.cwd or session_workdir,
    env=config.env,
    timeout=config.timeout,
    backend=backend,
    registry=SessionInstanceRegistry(storage, session_id),
)
```
`session_id` is already a factory parameter. Stateless prototypes get
`resume_command=None`; the tool then ignores the registry.

### 5.5 Router / API (`app/_router/_subagent.py` + `_schema/_subagent.py`)

Prototype CRUD is **unchanged in shape** — `resume_command` rides along in the
`data` dict and `/subagent/schemas` auto-reflects it (it is a plain new field on
`CliSubAgentConfig`). No new endpoints in v1 (the monitor is frontend-only;
forget is deferred, §9).

## 6. Frontend design

### 6.1 Left sidebar — prototype CRUD (`SubagentSidebar`)

New component `examples/web_ui/frontend/src/components/subagent/SubagentSidebar.tsx`,
following `TeamSidebar.tsx`'s structure: a self-contained
`<Sidebar collapsible="none" className="w-72 border-r">` (wider than TeamSidebar's
`w-56` because it hosts an edit form) inserted into `ChatPageInner`'s flex row
(`pages/chat/index.tsx`) **before** `<ChatViewport>`.

**Visibility (explicit — it is *not* conditional like TeamSidebar):** the sidebar
has its own toggle. Add a left-sidebar toggle control to the chat top bar (a
left-side analog of the existing right-panel dropdown in `ChatViewport`), backed
by a boolean UI state in `ChatPageInner`; **default hidden**. On mobile, render it
`collapsible="offcanvas"` like the session sidebar rather than inline.

Contents:
- Header + "New prototype" action.
- List of prototypes (from `useSubagents`), each with an inline expand → **edit
  form** and a delete control.
- Form fields: `name`, `description`, `command`, **`resume_command` (optional)**,
  `cwd`, **`env` (new; key/value rows)**, `timeout`. Client validation mirrors the
  backend: `command` must contain `{prompt}`; if `resume_command` is non-empty,
  both `command` and `resume_command` must contain `{prompt}` and `{agent_id}`.
- Create → `subagentApi.create({ data })`; edit → `subagentApi.update(id,
  { data })` (wire the currently-unused client method into `useSubagents` as an
  `update`).

Remove the old right-dock `SubagentPanel` (prototype list) and the
`AddSubagentDialog` modal.

### 6.2 Right dock — Instance Monitor (`SubagentInstanceMonitor`)

New component replacing `SubagentPanel` as the `subagent` `PanelDescriptor`
content in `ChatViewport`. **Pure frontend derivation over the session
transcript** — no new API:
- Walk the loaded message blocks; for each tool call whose `call.name` is a
  stateful sub-agent (matches a prototype whose `data.resume_command` is set) and
  whose input has an `instance` handle, group by `handle`.
- Per instance, collect ordered exchanges `{prompt (from input), output (from
  result), action ("new"/"resumed"), agent_id}`.
  - `agent_id` and `action` come from `result.metadata` (§3). **Fallback** if
    metadata is unavailable: `action` = "new" for the first occurrence of a
    handle in order, "resumed" afterwards; `agent_id` display relaxed.
- Render: an instance list (`handle · prototype · agent_id · #exchanges`);
  selecting one shows its interaction history, reusing the existing sub-agent
  body renderer for each exchange. Updates live as calls stream into the
  transcript.

### 6.3 Transcript badge

In `tool-renderers` (`DefaultRenderer.tsx` header path / `subagentRenderBody`),
when a sub-agent call is stateful, render a small badge next to the header:
`[<handle> · new]` / `[<handle> · resumed]`. `handle` from `call.input.instance`;
`new`/`resumed` from `result.metadata.action` (order-based fallback as in §6.2).
`subagentNameSet` and its context are unchanged.

### 6.4 API client / types / hooks

- `api/types.ts`: add `resume_command?: string | null` to `SubAgentData`.
- `useSubagents`: add an `update(id, data)` calling `subagentApi.update`.
- No new API surface for instances (derived from the transcript).

### 6.5 i18n (`en.json` + `zh.json`)

New keys (both locales): left sidebar title + form labels/hints for
`resume_command` and `env`; instance monitor title, empty state, column labels;
badge `new` / `resumed`. Reuse existing `panel.subagent.*` / `tool.*` keys where
they still apply; retire keys tied to the removed create modal
(`dialog-subagent-add.*`) once nothing references them.

## 7. Data flow — a stateful call

```
LLM: claude_code(prompt="draft intro", instance="writer")
  └─ CliSubAgentTool.call:
       resolve("writer","claude_code") → miss → agent_id=abc, created=True
       template = command
       argv = claude -p "draft intro" --permission-mode auto --session-id abc
       run → output; metadata = {instance:"writer", agent_id:"abc", action:"create"}

LLM: claude_code(prompt="now revise", instance="writer")
  └─ resolve("writer",…) → hit → agent_id=abc, created=False
       template = resume_command
       argv = claude -p "now revise" --permission-mode auto --resume abc
       run → output; metadata = {…, action:"resume"}

Frontend:
  transcript → [writer · new] then [writer · resumed] badges
  right dock  → instance "writer" (claude_code · abc) with 2 exchanges
```

## 8. Error handling

- **Execution:** reuse existing strings — `Sub-Agent exited with code N.\n…`,
  `Sub-Agent timed out after Ns.`, `Sub-Agent failed: …`; 30000-char truncation;
  timeout sentinel unchanged.
- **Validation:** malformed `command`/`resume_command` (missing placeholders,
  non-literal first token) → 422 on save (existing pydantic path).
- **Registry:** best-effort per §5.3 — degrade to a create run and log; never
  fail a call solely because the registry write failed.
- **Frontend:** a stateful call missing `instance` in its input (shouldn't happen
  — schema requires it) renders without a badge and is skipped by the monitor
  grouping.

## 9. Backward compatibility & deferred work

- **Backward compatible:** stored prototypes without `resume_command` are
  stateless; tool schema stays `{prompt}`; runtime and UI unchanged. No migration.
- **Deferred — forget/kill:** to reset a handle, add
  `DELETE /session/{session_id}/subagent-instances/{handle}` +
  `delete_subagent_instance` storage method + a monitor button. The registry key
  layout in §5.2 already supports this; left out of v1 to keep the change
  additive and the monitor read-only.
- **Deferred — persisted history:** if transcript-derived history proves
  insufficient (e.g. very long sessions, pagination), add a capped `history`
  array to `SubAgentInstanceRecord` and a `GET …/subagent-instances` read; the
  monitor would then prefer the endpoint over derivation.

## 10. Testing

**Backend (`tests/*_test.py`, whole-structure assertions, `AnyString` for uuids):**
- Config validators: stateless config unchanged; stateful requires `{prompt}` +
  `{agent_id}` in both templates; non-literal first token rejected; empty
  `resume_command` imposes no `{agent_id}` rule.
- `SessionInstanceRegistry.resolve`: first call mints a uuid + persists (created
  True); second call returns the same uuid (created False), against a fake/in-mem
  storage.
- `CliSubAgentTool` stateful path with a fake backend + fake registry: first call
  builds argv from `command` with the minted `{agent_id}`, second from
  `resume_command` with the **same** `agent_id`; result `metadata` carries
  `{instance, agent_id, action}`. Stateless path: schema is `{prompt}` and argv
  matches today.
- Storage round-trip for `SubAgentInstanceRecord` (upsert/get/list) under the new
  Redis keys.
- Factory: stateful config yields a tool with a registry + `resume_command`;
  stateless yields the current tool.

**Frontend:** typecheck-clean; component smoke test for the monitor grouping and
the badge if the harness supports it (otherwise rely on typecheck + manual).

## 11. File-by-file change list

**Backend**
- `src/agentscope/subagent/_base.py` — add `resume_command`; extend validators.
- `src/agentscope/subagent/_tool.py` — stateful path, generalized `_build_argv`,
  result metadata, stateful `input_schema`.
- `src/agentscope/subagent/_agent_tools.py` — pass `resume_command` + registry.
- `src/agentscope/subagent/__init__.py` — export `SessionInstanceRegistry` (and
  `SubAgentInstanceRecord` if it belongs to the public surface).
- `src/agentscope/app/storage/_model/_subagent_instance.py` — new record.
- `src/agentscope/app/storage/_base.py` + `_redis_storage.py` — instance methods
  + key layout.
- (No router change in v1.)

**Frontend**
- `src/components/subagent/SubagentSidebar.tsx` — new (prototype CRUD).
- `src/components/subagent/SubagentInstanceMonitor.tsx` — new (right-dock).
- `src/pages/chat/index.tsx` — mount the left sidebar in `ChatPageInner`.
- `src/pages/chat/ChatViewport.tsx` — swap the `subagent` panel content to the
  monitor; drop the old panel/modal wiring.
- `src/components/panel/SubagentPanel.tsx`, `src/components/dialog/AddSubagentDialog.tsx`
  — remove.
- `src/components/chat/tool-renderers/DefaultRenderer.tsx` — badge.
- `src/hooks/useSubagents.ts` — add `update`.
- `src/api/types.ts` — add `resume_command`.
- `src/i18n/locales/en.json`, `zh.json` — new/retired keys.

**Docs/tests**
- `tests/subagent_*_test.py` — extend per §10; new registry/storage tests.
