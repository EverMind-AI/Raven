# Per-Agent Sub-Agent ID Provisioning Mechanisms + Built-in Presets — Design

**Date:** 2026-07-17
**Status:** Approved (brainstorming → spec)
**Repo:** AgentScope 2.0 working copy (RavenX_demo)
**Builds on:** `2026-07-16-subagent-instances-design.md` (stateful prototype→instance split)

## 1. Overview

Different CLI agents mint and resume their session identity in different ways.
The current stateful sub-agent model (`2026-07-16-subagent-instances`) assumes
exactly **one** mechanism — the caller mints a uuid up front and injects it into
both the create and resume commands via `{agent_id}` (Claude Code's model). This
design generalizes the sub-agent layer to support a **second, fundamentally
different** mechanism where the CLI itself mints the session id and RavenX must
**parse it out of the create run's output** (Codex's model), and it ships the two
agents as canonical built-in **presets**.

Two id-provisioning strategies, expressed as one explicit config field
`id_source`:

1. **`provisioned`** (Claude Code, today's behavior, the default). RavenX mints a
   uuid, injects it into the create command via `{agent_id}`
   (`claude -p {prompt} --permission-mode auto --session-id {agent_id}`), and
   later resumes with the same id
   (`claude -p {prompt} --permission-mode auto --resume {agent_id}`).

2. **`derived`** (Codex, new). The create command carries **no** `{agent_id}` —
   the CLI assigns the session id itself
   (`codex -a never exec -s workspace-write -c 'sandbox_workspace_write.network_access=true' {prompt}`).
   RavenX learns the id **only after** a successful create run, by matching a
   configured `session_id_pattern` against the run's stdout, then resumes with it
   (`codex ... resume {agent_id} {prompt}`).

Separately — and orthogonally to identity — some CLIs wrap the agent's real reply
in a noisy transcript (Codex prints a header, a `user` echo, sandbox warnings, a
`codex` marker, then the reply, then a `tokens used` footer). A third optional
field `output_pattern` extracts just the reply from stdout so the main agent
receives clean output.

The whole change is **additive**: `id_source` defaults to `provisioned` and both
pattern fields default to `None`, so every existing stored prototype and the
Claude preset behave byte-for-byte as today. No data migration.

## 2. Goals / Non-goals

**Goals**
- One explicit strategy field `id_source: "provisioned" | "derived"` on
  `CliSubAgentConfig`, plus two optional regex fields `session_id_pattern`
  (identity) and `output_pattern` (reply extraction).
- Strategy-aware validation of the create/resume template pair.
- A `CliSubAgentTool` runtime that runs a two-phase create/resume lifecycle,
  parses the CLI-minted id for `derived` prototypes, and extracts the real reply
  when `output_pattern` is set.
- Registry persistence **deferred until a create run succeeds** (both strategies),
  which additionally fixes the known "failed create poisons the handle" follow-up.
- Two canonical built-in presets (Claude Code, Codex) defined in code, exposed via
  a read-only `GET /subagent/presets` endpoint, and instantiable from the
  `/subagents` page with one click.
- Backend test coverage (whole-structure `model_dump()` asserts) + frontend
  build/lint green.

**Non-goals**
- No new editable UI **form fields** for `id_source` / `session_id_pattern` /
  `output_pattern`. They are set by presets and carried invisibly through the
  edit form (pass-through), never rendered as inputs.
- No "forget/kill instance" endpoint (unchanged from the instances design).
- No auto-seeding of presets into user data; presets are instantiated on demand.
- No change to the DAG orchestrator, the Instance Monitor, or the transcript badge
  beyond what naturally flows through `CliSubAgentTool.call()`.

## 3. Architecture

### 3.1 Config model — `src/agentscope/subagent/_base.py`

Add three fields to `CliSubAgentConfig`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id_source` | `Literal["provisioned", "derived"]` | `"provisioned"` | Who mints the CLI session id. |
| `session_id_pattern` | `str \| None` | `None` | Regex with **exactly one** capture group, matched against the create run's stdout to extract the CLI-minted session id. Required iff `id_source == "derived"`. |
| `output_pattern` | `str \| None` | `None` | Regex with **exactly one** capture group; when set, `group(1)` of the first match against stdout is returned as the sub-agent reply. Applies to every run, both strategies. |

Validation (`_validate_stateful`, `mode="after"`, extended):

- **Stateless** (`resume_command` empty): `id_source` must be `"provisioned"`
  (reject `derived` without a resume command); `session_id_pattern` must be unset.
- **`provisioned` + stateful** (unchanged from today): both `command` and
  `resume_command` must contain `{agent_id}` and a prompt placeholder
  (`{prompt}` or `{prompt_file}`), and start with a literal executable.
- **`derived` + stateful** (new): `command` must **not** contain `{agent_id}`
  (the CLI assigns it) but must contain a prompt placeholder and start with a
  literal executable; `resume_command` **must** contain `{agent_id}` + a prompt
  placeholder + literal executable; `session_id_pattern` must be present, compile
  under `re.compile`, and have **exactly one** capture group (`.groups == 1`).
- `output_pattern`, when set (either strategy): must compile and have exactly one
  capture group.

Invalid regex (either pattern) or wrong group count raises a `ValueError` at
config-construction time, so a malformed preset/prototype never reaches runtime.

### 3.2 Tool runtime — `src/agentscope/subagent/_tool.py`

`CliSubAgentTool.__init__` gains `id_source: str = "provisioned"`,
`session_id_pattern: str | None = None`, `output_pattern: str | None = None`.
The two patterns are compiled once in `__init__` (store `self._session_id_re` /
`self._output_re`); `is_stateful` is unchanged.

`call()` stateful branch becomes a two-phase lifecycle:

1. `handle = instance or uuid.uuid4().hex`.
2. `existing = await registry.lookup(handle)` (read-only; `None` if unseen). On a
   registry exception, **fail closed**: yield a terminal ERROR chunk and return
   without running or committing anything, so an existing resumable session is
   never silently orphaned (the caller can retry).
3. **RESUME** (`existing is not None`): `agent_id = existing`,
   `template = resume_command`, `action = "resume"`, `created = False`.
4. **CREATE** (`existing is None`): `template = command`, `action = "create"`,
   `created = True`. If `provisioned`, mint `agent_id = uuid.uuid4().hex` up front;
   if `derived`, `agent_id = None` (unknown until parsed).

Build argv and run as today (`_build_argv` already no-ops `{agent_id}` when
`agent_id is None`, which matches a derived create command that omits it).

**On a successful run (`result.ok()`), in this order:**
1. **Parse the CLI-minted id** — if this was a **derived create**, match
   `self._session_id_re` against stdout. Match → `agent_id = m.group(1)`; no match
   → leave `agent_id = None`.
2. **Commit** — if `agent_id is not None` **and** this was a create,
   `await registry.commit(handle, agent_id, self.name)` (deferred persistence).
3. **Extract the reply** — if `self._output_re` is set,
   `output = m.group(1).strip()` for the first match against stdout; on no match,
   fall back to the raw combined stdout+stderr. When `output_pattern` is unset,
   keep today's behavior (stdout, with stderr appended).
4. **Append the parse-miss warning last** — if this was a derived create with no
   id parsed (step 1 miss), append to the (already extracted) `output`:
   `\n\n[RavenX] Warning: could not extract a session id from the create output;
   this instance is not resumable. Use a new instance handle to recreate.` Doing
   this after extraction guarantees the warning survives into the returned text.
5. Truncation (`_MAX_OUTPUT_CHARS`) and `output_file` writing use the final
   `output`.
6. `metadata = {"instance": handle, "agent_id": agent_id, "action": action}` (as
   today; `agent_id` may be `None` on a derived-create parse miss).

**On error / timeout / non-zero exit:** no id parse, no commit, no output
extraction; return the raw stdout+stderr for debugging (unchanged).

Stateless prototypes are unchanged: mint a throwaway uuid, run `command`, no
registry, no extraction unless `output_pattern` is set.

### 3.3 Registry — `src/agentscope/subagent/_instance_registry.py`

Replace the eager `resolve(handle, name) -> (agent_id, created)` with a two-phase
API:

- `async lookup(handle: str) -> str | None` — read-only; returns the stored
  `agent_id` or `None`. Never mints, never writes.
- `async commit(handle: str, agent_id: str, prototype_name: str) -> None` —
  persist `handle → agent_id` after a successful create.

Persistence is thus **deferred to after a successful create** for both strategies.
This is required for `derived` (the id doesn't exist until the run completes) and
is a strict improvement for `provisioned`: a failed create no longer persists the
handle, so a later same-handle call re-creates cleanly instead of resuming a CLI
session that was never established.

The storage duck-type (`get_subagent_instance` / `upsert_subagent_instance`) and
the `SubAgentInstanceRecord` model are unchanged.

### 3.4 Presets — new `src/agentscope/subagent/_presets.py`

Define the two canonical presets as data and expose them via a small helper:

- `list_subagent_presets() -> list[dict]` returns, per preset, a dict with a stable
  `preset_id`, a human `label`, and a `data` payload that is a valid
  `CliSubAgentConfig` dict **without** a persisted `id` (the create endpoint mints
  one). A unit test round-trips each preset's `data` through
  `SubAgentFactory.from_dict` to guarantee validity.

```
preset_id: "claude_code"   label: "Claude Code"
  type: cli_subagent   name: claude_code   id_source: provisioned
  command:        claude -p {prompt} --permission-mode auto --session-id {agent_id}
  resume_command: claude -p {prompt} --permission-mode auto --resume {agent_id}
  session_id_pattern: null   output_pattern: null

preset_id: "codex"         label: "Codex"
  type: cli_subagent   name: codex   id_source: derived
  command:        codex -a never exec -s workspace-write -c 'sandbox_workspace_write.network_access=true' {prompt}
  resume_command: codex -a never exec -s workspace-write -c 'sandbox_workspace_write.network_access=true' resume {agent_id} {prompt}
  session_id_pattern: (?im)^session id:\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})
  output_pattern:     (?s)\ncodex\n(.*?)\ntokens used
```

The Codex patterns were derived from real `codex v0.144.5` output:
```
OpenAI Codex v0.144.5
--------
...
session id: 019f7035-0a1c-7143-9931-b6488c43f0b3
--------
user
hello
warning: Codex could not find bubblewrap on PATH. ...
codex
Hello! What would you like to work on?
tokens used
2,859
```
`session_id_pattern` captures the dashed UUID on the `session id:` line;
`output_pattern` captures the slice between the `codex` marker and `tokens used`
(→ `Hello! What would you like to work on?`).

### 3.5 Preset API — `src/agentscope/app/_router/_subagent.py` (+ schema)

Add a read-only endpoint on the existing `subagent_router` (prefix `/subagent`):

- `GET /subagent/presets` → `ListSubAgentPresetsResponse { presets: [ { preset_id,
  label, data } ] }`. No user scoping (presets are global, static). Instantiation
  reuses the existing `POST /subagent/` create endpoint with `{ data: preset.data }`
  (the server mints the id and validates via `SubAgentFactory.from_dict`).

A new response schema `ListSubAgentPresetsResponse` (+ a `SubAgentPreset` item
model) is added to `_router/_schema/_subagent.py` and its `__init__` export.

### 3.6 Frontend — `examples/web_ui/frontend/`

No new editable form fields. Changes:

- `src/api/types.ts`: extend `SubAgentData` with the three optional fields
  (`id_source?: 'provisioned' | 'derived'`, `session_id_pattern?: string | null`,
  `output_pattern?: string | null`); add `SubAgentPreset` +
  `SubAgentPresetsResponse` types.
- `src/api/subagent.ts`: `listPresets: () => client.get<SubAgentPresetsResponse>('/subagent/presets')`.
- `src/pages/subagent/index.tsx`:
  - Load presets once and render an **"Add from preset"** dropdown/menu beside the
    `+` button. Selecting a preset calls `create(preset.data)` (existing hook path)
    and opens the newly created prototype for editing. This is a menu, not
    per-field inputs.
  - `FormState` gains three **carried** (non-rendered) fields: `id_source`,
    `session_id_pattern`, `output_pattern`. `toForm` reads them from `view.data`
    (defaulting `id_source` to `'provisioned'`); `toPayload` includes them so
    editing a Codex prototype's name/description/cwd/etc. does not silently strip
    its strategy.
  - Strategy-aware client validation mirroring the backend: `const derived =
    form.id_source === 'derived'`; a derived stateful `command` is **not** required
    to contain `{agent_id}` (`cmdOk = command.includes('{prompt}') && (!stateful ||
    derived || command.includes('{agent_id}'))`); `resumeOk` still requires
    `{prompt}` + `{agent_id}` in `resume_command` for any stateful prototype.
    (The backend remains the source of truth; the client check only avoids
    wrongly blocking a valid derived prototype.)
- i18n: add keys for the "Add from preset" control in `en.json` / `zh.json` with
  full parity.

## 4. Data flow

**Codex first call (create, derived):**
main agent → `codex` tool with `{prompt, instance: "writer"}` → `lookup("writer")`
→ `None` → run `command` (no id) → exit 0 → parse `session id:` → `agent_id =
019f70…` → `commit("writer", "019f70…", "codex")` → extract reply via
`output_pattern` → return clean reply, `metadata.action = "create"`.

**Codex second call (resume):** `lookup("writer")` → `019f70…` → run
`resume_command` with that id → extract reply → return, `metadata.action =
"resume"`.

**Claude first/second call (provisioned):** as today, except persistence now
happens on the create's success rather than eagerly at lookup.

## 5. Error handling

- **Derived create, parse miss (exit 0 but no id):** return the reply + a
  `[RavenX] Warning: …not resumable…` note; do **not** commit; a later same-handle
  call re-creates.
- **Create fails (non-zero / timeout):** no commit (handle stays unclaimed); return
  raw stdout+stderr.
- **`output_pattern` no match on success:** fall back to raw combined output (never
  lose the run's result).
- **Invalid regex / wrong group count in config:** rejected at config construction
  (422 from the create/update endpoint; presets are covered by a unit test so they
  can never ship broken).
- **Registry backend error during `lookup`:** fail closed — return an ERROR
  chunk and run/commit nothing, preserving any existing session mapping so the
  caller can retry (rather than starting a fresh session that would orphan it).

## 6. Backward compatibility

- `id_source` defaults to `"provisioned"`; both patterns default to `None`. Every
  existing stored prototype and the Claude preset are byte-identical in behavior.
- Registry method rename (`resolve` → `lookup` + `commit`) is internal to the
  `_`-prefixed module; only `CliSubAgentTool.call()` and the registry tests use it.
- Deferred persistence changes only the *timing* of a write on the success path and
  removes an incorrect write on the failure path — an improvement, not a
  regression.
- The DAG orchestrator, Instance Monitor, and transcript badge are unaffected; they
  consume `CliSubAgentTool.call()` output/metadata, whose shape is unchanged.

## 7. Testing

Backend (`pytest`, whole-structure `model_dump()` / tuple comparisons per repo
convention; `AnyString`/`AnyValue` for nondeterministic fields):

- `subagent_config_test.py`: **update the existing whole-dict dumps** to carry the
  three new keys; add cases — provisioned unchanged; derived valid; derived without
  `resume_command` rejected; derived `command` with `{agent_id}` rejected; derived
  without `session_id_pattern` rejected; `session_id_pattern`/`output_pattern` that
  don't compile or don't have exactly one group rejected.
- `subagent_instance_registry_test.py`: rewrite for `lookup`/`commit`; assert
  `lookup` returns `None` pre-commit, `commit` persists, `lookup` returns it after,
  and no write occurs on lookup.
- `subagent_tool_test.py`: derived create parses id + commits + extracts reply;
  derived create parse-miss → no commit + warning; provisioned create failure →
  handle NOT committed (poison-fix); resume path uses stored id; `output_pattern`
  extraction + fallback on no match; stateless + `output_pattern`.
- New `subagent_presets_test.py`: both presets round-trip through
  `SubAgentFactory.from_dict`; the Codex `session_id_pattern` matches the real
  header sample and `output_pattern` extracts the sample reply.
- `subagent_router_test.py`: `GET /subagent/presets` returns both presets
  (whole-structure).

Frontend (no JS test runner): `pnpm -C examples/web_ui/frontend build` (tsc -b +
vite) and `pnpm -C examples/web_ui/frontend lint` both green.

## 8. Resolved decisions

1. **Deferred persistence for `provisioned` too** — adopted (also fixes the
   poisoned-handle follow-up), rather than leaving it eager.
2. **Derived-create parse miss returns reply + warning, no persist** — adopted,
   rather than a hard error, so the completed work is never lost.
3. **`{agent_id}` forbidden in a derived `command`** — adopted, to enforce the
   mechanism explicitly rather than merely not requiring it.
4. **Lookup failure fails closed** (amended post-implementation review,
   2026-07-18) — a transient registry `lookup` error returns an ERROR chunk and
   runs/commits nothing, rather than degrading to a create run. A create would,
   for a `provisioned` prototype, mint a fresh id and silently overwrite an
   existing handle's mapping, orphaning a live resumable session; failing closed
   preserves the never-orphan invariant at the cost of also refusing to create a
   brand-new handle during a storage outage (a transient `lookup` failure cannot
   distinguish the two cases).
