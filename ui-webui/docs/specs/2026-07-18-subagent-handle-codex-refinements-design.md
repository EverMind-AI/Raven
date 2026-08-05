# Sub-Agent handle & Codex parsing refinements — design

**Date:** 2026-07-18
**Status:** Approved (design)
**Builds on:** `2026-07-17-subagent-id-mechanisms-design.md` (id_source, session_id_pattern, output_pattern, presets)

## 1. Motivation

Three independent refinements to the CLI sub-agent layer, driven by real
runtime failures and usability gaps:

1. **Semantic instance handles.** The `instance` handle already maps to a
   persisted CLI session id (via `SessionInstanceRegistry`) and is
   auto-converted at call time, but nothing tells the main agent to choose a
   *meaningful* name. Agents pick opaque strings, hurting the readability of
   the `/subagents` instance monitor and of DAG specs.
2. **Claude Code session-id bug (confirmed).** Creating a Claude Code
   instance fails at runtime:
   `Sub-Agent exited with code 1. Error: Invalid session ID. Must be a valid
   UUID.` The provisioned-create path mints `uuid.uuid4().hex` — 32 hex chars
   with **no dashes** — which is not a canonical UUID. Claude Code's
   `--session-id` rejects it.
3. **Fragile Codex regex parsing.** Codex session-id and reply extraction use
   line-oriented regexes against a human-formatted transcript. This is
   brittle. Codex supports `--json`, which emits a stable, machine-readable
   JSONL stream. Parsing that structurally removes the regex risk.

## 2. Scope

**In scope:** the three changes above, their validation, wiring, presets,
frontend field carry-through, and tests.

**Out of scope / explicitly unchanged:**
- No change to the DAG runner or the DAG `{prompt_file}` requirement. Codex
  stays on inline `{prompt}`, so (as today) it does not participate in DAG
  nodes. This is intentional per the design decision in §8.1.
- No new *visible* frontend form inputs (consistent with the id-mechanisms
  decision: strategy fields ride invisibly through the form).
- No router request/response schema change: `CreateSubAgentRequest.data` /
  `UpdateSubAgentRequest.data` are plain `dict`s validated against
  `CliSubAgentConfig` at factory time, so a new config field needs no schema
  edit.

## 3. Change 1 — Semantic handle guidance (prompt-only)

No behavior, registry, or storage change. Two description edits so the main
agent is instructed to pick semantic handles:

- **`CliSubAgentTool._build_input_schema`** (`subagent/_tool.py`): the
  `instance` property `description` gains guidance to use a short, semantic
  name reflecting the instance's role (e.g. `researcher`, `db-migrator`)
  rather than a random string, while preserving the existing "reuse the same
  handle to continue the same conversation; use a new handle for a fresh
  instance" semantics.
- **`SubAgentDagTool`** (`subagent/_dag/_tool.py`): the
  `_NODE_SCHEMA["instance"]` `description` and the `instance` sentence in
  `_DEFAULT_DESCRIPTION` gain the same semantic-naming hint.

The mapping (`handle -> agent_id`) and auto-conversion (lookup ⇒ resume) are
already implemented and remain unchanged.

## 4. Change 2 — Canonical UUID for provisioned session ids

In `CliSubAgentTool.call` (`subagent/_tool.py`):

- Provisioned-create id: `uuid.uuid4().hex` → `str(uuid.uuid4())` (canonical
  `8-4-4-4-12` form with dashes).
- Stateless id: `uuid.uuid4().hex` → `str(uuid.uuid4())` as well
  (defense-in-depth — a stateless `command` may legally contain `{agent_id}`;
  the validator does not forbid it).

Unchanged (never used as a CLI session id, so `.hex` is fine):
- The default registry **handle** when `instance` is omitted.
- The temp-prompt filename.

## 5. Change 3 — Codex `--json` structured JSONL parsing

### 5.1 New config field

Add to `CliSubAgentConfig` (`subagent/_base.py`):

```python
transcript_format: Literal["text", "codex_jsonl"] = "text"
```

- `"text"` (default): today's regex behavior. `session_id_pattern` extracts
  the derived id; `output_pattern` extracts the reply. Fully backward
  compatible — every existing config keeps this value.
- `"codex_jsonl"`: parse stdout as JSONL (see §5.2). The regex fields are
  superseded and MUST be unset.

`transcript_format` is orthogonal to `id_source`: it governs *how the
transcript is read*, not *who mints the id*.

### 5.2 New parser module

New file `src/agentscope/subagent/_transcript.py`, a pure, side-effect-free,
independently testable function:

```python
def parse_codex_jsonl(stdout: str) -> tuple[str | None, str | None]:
    """Parse a Codex `exec --json` transcript.

    Returns (thread_id, reply_text):
      - thread_id: the `thread_id` of the FIRST line whose `type` is
        "thread.started"; None if absent.
      - reply_text: the `item.text` of the LAST `item.completed` event whose
        `item.type` is "agent_message"; None if absent.
    """
```

Behavior / robustness rules:
- Iterate `stdout.splitlines()`; strip each line; skip empty lines.
- `json.loads` each line; on `json.JSONDecodeError`/`ValueError`, skip that
  line (tolerate non-JSON banner/log lines).
- Skip parsed values that are not `dict`.
- `thread_id`: taken from the first `type == "thread.started"` object with a
  string `thread_id`; later `thread.started` lines are ignored.
- `reply_text`: scan all objects; for each `type == "item.completed"` whose
  `item` is a dict with `item["type"] == "agent_message"` and a string
  `item["text"]`, record `text`, keeping the **last** such value.
- Missing/extra fields never raise — they yield `None` for that component.

Example (from the real Codex `v0.144.x` `--json` sample in this design's
source conversation): first line
`{"type":"thread.started","thread_id":"019f75ec-..."}` → `thread_id`; the
final `item.completed` with `item.type=="agent_message"` (item_4) → its
`text`. Intermediate `command_execution` `item.completed` events are skipped.

### 5.3 Runtime integration (`CliSubAgentTool`)

- `__init__` gains `transcript_format: str = "text"`, stored as
  `self._transcript_format`.
- In `call`, on a successful exit, when `self._transcript_format ==
  "codex_jsonl"`, call `parse_codex_jsonl(stdout)` once to get
  `(jsonl_id, jsonl_reply)`. Then:
  - **Derived-create id:** if `created and self._id_source == "derived"`, set
    `agent_id = jsonl_id` when `jsonl_id is not None` (replacing the
    regex-search branch for this format).
  - **Reply extraction:** `output = jsonl_reply.strip()` when `jsonl_reply is
    not None`, else the combined stdout+stderr fallback (same fallback the
    regex path uses).
- The `text` regex path is unchanged and used when `transcript_format ==
  "text"`.
- The deferred `commit` (create + known id only) and the "could not extract a
  session id … not resumable" warning (derived-create with `agent_id is
  None`) are format-independent and continue to work for `codex_jsonl`.

### 5.4 Validation (`CliSubAgentConfig`)

- **New rule:** when `transcript_format == "codex_jsonl"`, both
  `session_id_pattern` and `output_pattern` MUST be `None`
  (`ValueError` otherwise) — the structured parser supersedes them.
- **Relaxed rule:** the derived branch of `_validate_stateful` requires a
  `session_id_pattern` **only when** `transcript_format == "text"`. With
  `codex_jsonl`, the derived id comes from the JSONL, so no pattern is
  required (and, by the rule above, none is allowed).
- Unchanged: stateless ⇒ provisioned & no `session_id_pattern`; provisioned
  stateful ⇒ `{agent_id}` in both templates; derived ⇒ no `{agent_id}` in
  `command`, `{agent_id}` in `resume_command`; regex group-count checks in
  `_validate_patterns` still apply to any non-None pattern.

### 5.5 Codex preset (`subagent/_presets.py`)

- Commands add `--json`:
  - create: `codex -a never exec -s workspace-write -c
    'sandbox_workspace_write.network_access=true' --json {prompt}`
  - resume: `codex -a never exec -s workspace-write -c
    'sandbox_workspace_write.network_access=true' --json resume {agent_id}
    {prompt}`
- Set `transcript_format: "codex_jsonl"`.
- Set `session_id_pattern: None` and `output_pattern: None` (remove the two
  regex constants, now unused).
- `id_source` stays `"derived"`.

### 5.6 Factory wiring (`subagent/_agent_tools.py`)

`make_subagent_tool_factory` forwards `transcript_format=config.
transcript_format` into the `CliSubAgentTool(...)` constructor.

### 5.7 Frontend carry-through

Add the field so an edit→save round-trip does not silently drop it (same
invisible-carry pattern as `id_source`/`session_id_pattern`/
`output_pattern`):

- `api/types.ts`: `SubAgentData` gains `transcript_format?: string`.
- `pages/subagent/index.tsx`: `FormState`, `EMPTY_FORM`, `toForm`,
  `toPayload` carry `transcript_format` invisibly. No new rendered input; no
  new i18n key. Add-from-preset POSTs raw `preset.data`, so it already
  carries the field with no code change.

## 6. Testing

- **`tests/subagent_transcript_test.py` (new):** parser unit tests —
  (a) canonical sample → correct `thread_id` + last-agent-message text;
  (b) multiple `agent_message` completions → last wins;
  (c) interleaved `command_execution` completions → skipped;
  (d) missing `thread.started` → `(None, reply)`;
  (e) malformed/non-JSON lines interspersed → tolerated;
  (f) empty stdout → `(None, None)`.
  Assert whole tuples.
- **`tests/subagent_config_test.py`:** `transcript_format` validation matrix
  (codex_jsonl forbids both patterns; derived+codex_jsonl needs no pattern;
  derived+text still requires session_id_pattern; default is `"text"`), plus
  the whole-dict `model_dump()` assertions updated for the new field.
- **`tests/subagent_factory_test.py`:** update the whole-dict `model_dump()`
  round-trip assertions for the new field (plan gap: dumps live in BOTH this
  file and `subagent_config_test.py`).
- **`tests/subagent_tool_test.py`:** codex_jsonl create parses id from JSONL
  and commits it; resume reuses it; reply is the last agent_message;
  parse-miss (no `thread.started`) appends the not-resumable warning; AND
  update any test asserting the pre-fix dashless-hex provisioned `agent_id`
  format to accept a canonical UUID.
- **`tests/subagent_presets_test.py`:** codex preset carries `--json`,
  `transcript_format == "codex_jsonl"`, and null patterns; round-trips
  through `SubAgentFactory.from_dict`.
- **`tests/subagent_router_test.py`:** preset endpoint reflects the codex
  changes (if it asserts codex fields).
- **`tests/subagent_agent_tools_test.py`:** factory forwards
  `transcript_format` (assert `_transcript_format` on the built tool).
- **Frontend:** no JS test runner — `pnpm build` (tsc + vite) + `eslint` +
  i18n parity must stay green.

## 7. Files touched

- `src/agentscope/subagent/_base.py` — field + validation.
- `src/agentscope/subagent/_tool.py` — canonical UUID (Change 2) +
  `transcript_format` runtime (Change 3) + semantic `instance` description
  (Change 1).
- `src/agentscope/subagent/_transcript.py` — **new** parser.
- `src/agentscope/subagent/_presets.py` — codex preset.
- `src/agentscope/subagent/_agent_tools.py` — factory wiring.
- `src/agentscope/subagent/_dag/_tool.py` — semantic `instance` description
  (Change 1).
- `examples/web_ui/frontend/src/api/types.ts` — `SubAgentData` field.
- `examples/web_ui/frontend/src/pages/subagent/index.tsx` — invisible carry.
- Tests as listed in §6.

No change to: the DAG runner, the instance registry, the router routes or
schemas, or storage.

## 8. Decisions

1. **Codex stays inline-`{prompt}` (no DAG participation).** Making Codex a
   DAG node would require either a `{prompt_file}` Codex command or relaxing
   the DAG's `command_uses_prompt_file` guard. Out of scope; the user chose
   the prompt-only path for Change 1. (2026-07-18)
2. **Semantic handles are prompt-only.** The persisted map + auto-conversion
   already exist; only the guidance was missing. No node-id/handle unification
   (that would break reusing one instance across multiple DAG nodes, which
   need distinct node ids but a shared handle). (2026-07-18)
3. **`transcript_format` as an orthogonal discriminator**, not a boolean or an
   `id_source` variant. It cleanly separates transcript parsing from id
   provisioning and leaves room for future structured formats. For
   `codex_jsonl` it drives BOTH id and reply extraction, so the regex fields
   are forbidden. (2026-07-18)
4. **Reply = last `agent_message`**, not the literal last `item.completed`
   (which may be a `command_execution` with no reply text). (2026-07-18)
5. **Canonicalize both create-time ids** (provisioned + stateless) but leave
   the internal handle and temp filename as `.hex` — only ids that reach a CLI
   `--session-id` must be canonical UUIDs. (2026-07-18)
