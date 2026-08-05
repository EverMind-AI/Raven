# Per-Session Working Directory + Sub-Agent Call Labeling — Design

**Date:** 2026-07-16
**Status:** Approved (brainstorming → spec)
**Repo:** AgentScope 2.0 working copy (RavenX_demo)

## 1. Overview

Two related improvements to the AgentScope app service + web UI:

1. **Per-session working directory.** Each chat session gets its own physical
   working directory under the existing `workspaces/` base. Within a session,
   the main (unified) agent **and** its CLI sub-agents run in that one directory
   by default ("共用" — shared among a session's agents). A control below the
   chat input lets the user **override** the session's working directory to any
   absolute path (e.g. point sub-agents at an existing repo). Each CLI sub-agent
   may still pin its own `cwd`, which wins over the session directory.

2. **Sub-Agent call labeling.** In the web UI, a tool call that dispatches to a
   registered CLI sub-agent is labeled **"Call Sub-Agent" / "调用子智能体"**
   instead of the generic **"Call tool" / "调用工具"**.

This builds directly on the existing `workspace` machinery: a session already
carries an authoritative `workspace_id`, and the main agent's built-in tools
already execute in `workspace.workdir`. The core of item 1 is (a) making the
on-disk layout honor per-session isolation, and (b) making the CLI sub-agent
tool default its `cwd` to that same directory.

## 2. Goals / Non-goals

**Goals**
- Each session resolves to its own directory `workspaces/<workspace_id>/`
  (per-session isolation), instead of all of an agent's sessions sharing
  `workspaces/<agent_id>/`.
- A per-session, user-settable working-directory **override** (any absolute
  path), edited from a control **below the chat box**, persisted via
  `PATCH /sessions/{id}`.
- The CLI sub-agent tool defaults its working directory to the session's
  effective working directory; a per-sub-agent `cwd` (already in the Add
  Sub-Agent dialog) still takes precedence.
- The web UI labels sub-agent tool calls as "Call Sub-Agent".
- Keep the change small and mostly additive; the only edit to shared workspace
  internals is a one-line layout fix that makes the existing (but currently
  inert) `IsolationPolicy.PER_SESSION` actually work.

**Non-goals (v1)**
- Renaming the `workspaces/` base directory (kept as-is; UI just calls it
  "Working directory").
- Non-local workspace backends (Docker/E2B/etc. already store their own
  `workdir`; this design only needs the local path override to reach them via
  the same `get_workspace` param, and we do not add new behavior there).
- Giving `LocalBackend` a base `cwd` for `Read`/`Write`/`Edit`/`Grep`/`Glob`
  (they already resolve against absolute paths; only `Bash` is rooted at
  `workdir`, which is sufficient).
- A graphical directory picker (plain absolute-path text input only).
- Eagerly creating the session folder at session-create time (it is created
  lazily on first use by `LocalWorkspace.initialize()`, which already
  `makedirs` the path).

## 3. Background — current state (verified)

- **`SessionConfig`** (`src/agentscope/app/storage/_model/_session.py`) carries a
  required `workspace_id` — "the authoritative workspace binding for the
  session" and the cache key for `get_workspace`. It has **no** working-directory
  path field. `SessionConfig` is mutable via `PATCH /sessions/{id}`.
- **`LocalWorkspaceManager.get_workspace(...)`**
  (`src/agentscope/app/workspace_manager/_local_workspace_manager.py`) computes
  the on-disk folder at line ~131 as `os.path.join(self._basedir, agent_id)` —
  **keyed by `agent_id`**, not `workspace_id`. The cache is keyed by
  `workspace_id`, but two sessions of one agent still resolve to the *same*
  physical folder. Result: `IsolationPolicy.PER_SESSION` is effectively a no-op
  today (a latent bug). The deprecated `create_workspace` (line ~162) has the
  same layout.
- **Isolation default** is `PER_AGENT` (`workspace_manager/_base.py`); the
  reference service (`examples/agent_service/main.py`) constructs
  `LocalWorkspaceManager(basedir=<service>/workspaces, ...)` with no `isolation=`,
  so it runs `PER_AGENT`.
- **Chat turn** (`src/agentscope/app/_service/_chat.py`) resolves
  `workspace = get_workspace(user_id, agent_id, session_id, config.workspace_id)`,
  injects `workspace.workdir` into the permission context as an allowed working
  directory (`source="session"`), and builds the toolkit whose `Bash` is
  `Bash(cwd=workspace.workdir)` (`workspace/_base.py` `list_tools`).
- **CLI sub-agent** (`src/agentscope/subagent/`): `CliSubAgentTool` takes a
  `cwd` (default `None`); `make_subagent_tool_factory` in `_agent_tools.py`
  resolves the session's workspace backend but constructs the tool with only the
  sub-agent config's own `cwd`. So with no `cwd` set, the sub-agent inherits the
  **service process CWD**, not the session workspace.
- **Frontend** (`examples/web_ui/frontend/`): per-session settings persist via
  `PATCH /sessions/{id}` (the `handlePermissionModeChange` pattern in
  `ChatViewport.tsx`). The chat box (`TextInput`) is the last child of
  `ChatContent`'s flex column; there is no slot below it today (`footerSlot`
  renders above it). Tool-call labels are decided in
  `tool-renderers/DefaultRenderer.tsx` (`defaultRenderHeader` → `t('tool.callGeneric')`
  + `call.name`). A tool call carries **no** type/source metadata to mark a
  sub-agent — the only signal is the tool `name` string, and the set of
  sub-agent names is available globally from the `useSubagents()` hook.

## 4. Design

### 4.1 Effective working directory & precedence

For any tool execution in a session, the **effective working directory** is
resolved by this precedence:

1. **Per-sub-agent `cwd`** — only for a CLI sub-agent whose config sets `cwd`
   (existing field, existing Add-dialog input). Wins for that sub-agent.
2. **Session working-directory override** — `SessionConfig.work_dir`, if set
   (any absolute path). Applies to the main agent's tools and to sub-agents
   without their own `cwd`.
3. **Default session directory** — `workspaces/<workspace_id>/` (the resolved
   `workspace.workdir`). Used when neither of the above is set.

The main agent and sub-agents thus share one directory per session (precedence
2 or 3), and either can be redirected (precedence 1 for a sub-agent; precedence
2 for the whole session).

### 4.2 Backend

**(a) Make per-session isolation real (single shared-internals edit).**
- In `LocalWorkspaceManager.get_workspace`, compute the workdir keyed by
  **`workspace_id`**: `os.path.join(self._basedir, workspace_id)` (and the same
  in the deprecated `create_workspace`). This makes the on-disk layout follow
  whatever `IsolationPolicy` is configured. Under `PER_AGENT` the
  `workspace_id` is a deterministic hash of the agent, so behavior is stable
  (only the folder *name* changes from `<agent_id>` to the hash); under
  `PER_SESSION` each session gets a distinct folder.
- In `examples/agent_service/main.py`, construct
  `LocalWorkspaceManager(basedir=<service>/workspaces,
  isolation=IsolationPolicy.PER_SESSION, default_mcps=...)`. The library default
  stays `PER_AGENT`; only the reference service opts in.

**(b) Per-session working-directory override field.**
- Add `work_dir: str | None = None` to `SessionConfig` with a validator: if set,
  the value must be an absolute path (`os.path.isabs`), else raise (→ 422). A
  blank/omitted value means "use the default session directory".
- Add `work_dir` to `UpdateSessionRequest` (`_router/_schema/_session.py`) so the
  control below the chat box can PATCH it. The existing PATCH merge semantics
  (`model_dump(exclude_unset=True)`) already handle partial updates; clearing the
  override is done by PATCHing `work_dir: null`.

**(c) Honor the override in `get_workspace`.**
- Add an optional keyword `workdir: str | None = None` to
  `WorkspaceManagerBase.get_workspace` and `LocalWorkspaceManager.get_workspace`
  (additive; existing callers unaffected). When provided and non-empty, it wins
  over the computed `basedir/<workspace_id>`. The other five manager
  implementations (Docker/E2B/Daytona/K8s/OpenSandbox) override `get_workspace`;
  each gains the same optional `workdir` kwarg to keep signatures consistent with
  the abstract base — accept-and-ignore for v1 (only the local manager acts on
  it), so their behavior is unchanged.
- Cache consistency: `get_workspace` is cached by `workspace_id`. When a cached
  workspace's `workdir` differs from the requested override (i.e. the user just
  changed it), close and rebuild that cache entry so the new directory takes
  effect on the next turn.

**(d) Thread the override from the session into both callers.**
- `_service/_chat.py`: pass `workdir=session_record.config.work_dir` into
  `get_workspace`. The already-present permission-context injection then uses the
  effective `workspace.workdir` (override or default), so a custom path is
  auto-allowed and the main agent's `Bash` runs there.
- `subagent/_agent_tools.py`: pass `workdir=session.config.work_dir` into
  `get_workspace`, read the resolved `workspace.workdir`, and construct each
  `CliSubAgentTool` with `cwd = subagent_config.cwd or workspace_workdir`. This
  yields precedence §4.1 (per-sub-agent `cwd` wins; otherwise the session dir).
  The existing best-effort fallback (workspace resolution fails → bare
  `LocalBackend`, `cwd` = sub-agent config `cwd` or `None`) is preserved.

### 4.3 Frontend

**(a) Working-directory control below the chat box.**
- Add a new optional slot rendered **after** `<TextInput/>` in `ChatContent.tsx`
  (a new prop analogous to `footerSlot`, but placed below the input). Supplied
  from `ChatViewport.tsx`.
- The control is a compact row: a label ("Working directory" / "工作目录") and a
  path input. It shows `session.config.work_dir` when set, and a muted
  placeholder ("Default session workspace" / "默认会话工作目录") when unset. On
  save it calls `sessionApi.update({ work_dir })` following the
  `handlePermissionModeChange` pattern; clearing the field PATCHes `work_dir:
  null` to revert to the default.
- Add `work_dir?: string | null` to the `SessionConfig` and
  `UpdateSessionRequest` TypeScript types (`src/api/types.ts`).
- Note: the UI does not surface the resolved default path
  (`workspaces/<workspace_id>/`) when the override is unset — that path is
  server-side; the placeholder communicates "using the default".

**(b) "Call Sub-Agent" label.**
- Provide the set of registered sub-agent names via a lightweight React context
  (`SubagentNamesContext`) populated from `useSubagents()` at the
  `ChatViewport`/`ChatContent` level, so the deep tool-renderers can read it
  without prop-drilling.
- In `DefaultRenderer.tsx` `defaultRenderHeader`: if `call.name` is in the
  sub-agent name set, render `t('tool.callSubagent')` ("Call Sub-Agent" /
  "调用子智能体") instead of `t('tool.callGeneric')`. `defaultGetDisplayName`
  still shows the sub-agent's name as the argument text.
- In `MessageBubble.tsx` `summarizeToolGroup`: add a sub-agent category so the
  collapsed group summary reads e.g. "Delegated to N sub-agents" / "调用了 N 个子智能体"
  (new `tool.summary.subagent_*` keys) rather than the generic
  "called N tools" fallback.
- Add the new i18n keys to `src/i18n/locales/en.json` and `zh.json`
  (`tool.callSubagent`, `tool.summary.subagent_one`/`_other`), preserving en/zh
  parity.
- Best-effort by design: a call to a since-deleted sub-agent (absent from the
  live `useSubagents()` set) falls back to "Call tool". Acceptable.

## 5. File changes (summary)

**Backend**
- `src/agentscope/app/workspace_manager/_local_workspace_manager.py` — key
  workdir by `workspace_id` (and deprecated `create_workspace`); accept the new
  `workdir` override param + cache-rebuild-on-change.
- `src/agentscope/app/workspace_manager/_base.py` — add `workdir` kwarg to the
  abstract `get_workspace` signature/docstring.
- `src/agentscope/app/storage/_model/_session.py` — add `work_dir` field +
  absolute-path validator to `SessionConfig`.
- `src/agentscope/app/_router/_schema/_session.py` — add `work_dir` to
  `UpdateSessionRequest`.
- `src/agentscope/app/_service/_chat.py` — pass `workdir=config.work_dir` to
  `get_workspace`.
- `src/agentscope/subagent/_agent_tools.py` — pass `workdir=config.work_dir` to
  `get_workspace`; default sub-agent `cwd` to `workspace.workdir`.
- `examples/agent_service/main.py` — `isolation=IsolationPolicy.PER_SESSION`.

**Frontend**
- `examples/web_ui/frontend/src/components/chat/ChatContent.tsx` — slot below
  `<TextInput/>`.
- `.../src/pages/chat/ChatViewport.tsx` — render the working-directory control,
  wire the `sessionApi.update` handler, provide `SubagentNamesContext`.
- `.../src/components/chat/WorkingDirectoryControl.tsx` (new) — the control.
- `.../src/components/chat/tool-renderers/DefaultRenderer.tsx` — sub-agent label
  branch.
- `.../src/components/chat/MessageBubble.tsx` — sub-agent group-summary category.
- `.../src/api/types.ts` — `work_dir` on `SessionConfig` / `UpdateSessionRequest`.
- `.../src/i18n/locales/en.json`, `zh.json` — new keys.

## 6. Behavior changes & risks

- **Sessions become filesystem-isolated** in the reference service. Previously
  all sessions of an agent shared files under `workspaces/<agent_id>/`; now each
  session gets `workspaces/<workspace_id>/`. This is the intended behavior but is
  a change; existing on-disk folders from prior runs are effectively orphaned
  (harmless for the demo).
- The line-~131 change alters the default folder **name** even under `PER_AGENT`
  (from `<agent_id>` to the workspace-id hash). Any test asserting a
  `basedir/<agent_id>` path must be updated — the plan will grep for and fix
  these.
- A custom override path is created if missing and added to the permission
  context as allowed. Pointing a sub-agent at a sensitive path is the user's
  responsibility (consistent with the existing auto-run sub-agent model).
- Sub-Agent labeling is best-effort by tool name (§4.3b).

### 6.1 Accepted trade-offs (from the final whole-branch review)

These are acceptable for the local demo but must be revisited before this
pattern is deployed multi-tenant on the local backend:

- **`work_dir` is a trust input, not a sandbox boundary.** The absolute-path
  rule is a *correctness* guard only. The resolved dir is added to the
  permission context (`source="session"`) and used as sub-agent `cwd`; on the
  `LocalBackend` there is no FS sandbox, and auto-run sub-agents already run
  arbitrary CLIs — so `work_dir` does not widen the existing trust boundary in
  the demo. If promoted to a shared multi-tenant deployment on the local
  backend, an arbitrary absolute `work_dir` (e.g. `/`, another tenant's
  `workspaces/<id>`) would be a cross-tenant/host escalation vector and must be
  canonicalized/gated against `basedir`. Sandboxed backends (Docker/E2B/K8s)
  ignore `workdir`, so they are unaffected.
- **`permission_context.working_directories` is add-only.** Each distinct
  `work_dir` set during a session's life accumulates as a permanently-allowed
  directory; clearing/changing `work_dir` does not revoke previously-allowed
  paths. Pre-existing add-only behavior; low impact for the demo.
- **A mid-turn `work_dir` change can rebuild an in-use workspace.** The
  evict-and-rebuild-on-mismatch path means a workspace-resolving request with a
  new `work_dir` fired while a chat turn's tools are still bound to the old
  workspace would close that workspace (and its MCP gateway) mid-turn. Narrow —
  sessions normally process turns sequentially.

**Follow-up (optional hardening):** in `LocalWorkspaceManager.get_workspace`
Phase 3, if `initialize()` raises after the mismatched `orphan` is popped, the
orphan (and half-built workspace) is leaked because `_safe_close(orphan)` is
never reached. Extremely narrow (concurrent different-`workdir` build **and** an
init failure) and consistent with the module's pre-existing
no-cleanup-on-init-failure behavior; move the close into a `finally` if this
path ever matters.

## 7. Testing

- **Workspace manager:** two sessions (distinct `workspace_id`) under
  `PER_SESSION` resolve to distinct folders; the `workdir` override wins over the
  computed path; changing the override rebuilds the cached workspace so
  `workspace.workdir` reflects the new path.
- **Session schema/PATCH:** `SessionConfig.work_dir` accepts an absolute path and
  rejects a relative one (validator → 422 through the router); PATCH sets and
  clears it.
- **Sub-Agent factory:** with no sub-agent `cwd`, the built tool's `cwd` equals
  the session's `workspace.workdir` (default and override cases); with a
  sub-agent `cwd` set, it wins.
- **Frontend:** `tsc -b` clean; en/zh key parity; a manual check that a
  sub-agent call renders "Call Sub-Agent" and the group summary counts
  sub-agents separately.
- Tests follow repo conventions: whole-structure assertions, `AnyString`/
  `AnyValue` for nondeterministic fields, `unittest.IsolatedAsyncioTestCase` +
  fakeredis / FastAPI `TestClient` where applicable.

## 8. Out of scope

Non-local backends' own workdir semantics; `LocalBackend` base-cwd for
non-`Bash` builtins; a directory picker; cross-session/agent sharing controls;
eager session-folder creation. These are noted as possible follow-ups and are
not required for this feature.
