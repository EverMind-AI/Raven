# Unified `/subagents` page + stateful third-party CLI sub-agents

Date: 2026-07-27
Status: approved, not yet implemented

## 1. Problem

The web UI ships two sub-agent pages that look alike and do entirely different
things:

| | `/subagents` | `/raven-subagents` |
|---|---|---|
| Concept | AgentScope (inherited from RavenX) | Raven (P4) |
| Storage | Redis, per-user, per-record id | `~/.raven/config.json` -> `subagents.thirdParty` |
| Channel | direct service REST `/subagent/` | gateway WS proxy, validated + hot-applied |
| Write granularity | per-record CRUD | whole-list PUT |
| Reaches the Raven runtime | only via `dispatch_agents.json` in bridge mode | yes |

`/raven-subagents` is the only page that can make `spawn(agent=...)` and
`run_subagent_dag` work, but its form is a thin single-column stub. `/subagents`
has the good two-pane editor but writes to storage the Raven kernel never reads.

Separately, Raven's third-party CLI sub-agent is stateless: every spawn is a
fresh process, so a multi-turn delegation to Claude Code or Codex cannot
continue an earlier session.

## 2. Goals

1. One page at `/subagents` with the existing two-pane design, backed by the
   Raven config data plane (gateway proxy, whole-list PUT, hot-apply).
2. Raven gains stateful third-party CLI sub-agents: create-vs-resume driven by a
   caller-supplied instance handle, with the CLI session id persisted.
3. Presets for `claude_code`, `codex`, and `mirothinker` carry complete,
   directly-runnable commands.
4. The chat right-dock instance monitor reflects the same configuration and the
   persisted instance registry.

## 3. Non-goals

- Redacting `api_key` on the wire. `get_third_party_subagents` deliberately
  round-trips raw values so a read-edit-write cycle never clobbers a key
  (`raven/config/update_subagents.py:8-9`). The browser already receives it
  today; this change does not widen that exposure.
- A "forget instance" control in the UI. The registry self-prunes; an explicit
  delete can follow if it turns out to be needed.
- Sandboxing third-party CLI agents. They still run on the host because they
  need the host's auth, config, and PATH (`cli_agent.py:5-6`).

## 4. Decisions

| Question | Decision |
|---|---|
| Fields absent from Raven's schema | Extend the Raven schema rather than drop them |
| Instance registry location | `~/.raven/subagent_instances.json` |
| Instance lifetime | Tied to the chat session: no time-based expiry, deleted when the session is deleted |
| Who names an instance | The main agent, via a semantic `instance` handle on `spawn`; omitted means a fresh session |
| OpenAI api key entry | Inline password field; blank on edit keeps the stored value |
| Preset commands | Ported verbatim from the RavenX presets, plus `--output-format stream-json --verbose` for `claude_code` |
| `/raven-subagents` | Page, route, and sidebar entry deleted |

## 5. Backend design

### 5.1 Schema — `raven/config/schema.py`

`ThirdPartyCliSubagentConfig` gains five fields. `Base` uses a `to_camel` alias
generator, so the wire form is camelCase and both spellings are accepted on
write.

```python
resume_command: str | None = None       # resumeCommand; non-empty => stateful
id_source: Literal["provisioned", "derived"] = "provisioned"
session_id_pattern: str | None = None   # derived + text: group(1) is the id
output_pattern: str | None = None       # extract the reply from stdout
transcript_format: Literal["text", "codex_jsonl", "claude_stream_json"] = "text"
```

A `model_validator(mode="after")` rejects unrunnable commands at write time
instead of at spawn time. This is the whole reason for extending the schema
rather than storing free text: a `{agent_id}` that nothing substitutes is passed
to the CLI literally and the run fails with a confusing error.

| Condition | Constraint |
|---|---|
| stateless (`resume_command` empty) | `{agent_id}` must not appear in `command` |
| stateful + `provisioned` | `command` must contain `{agent_id}` |
| stateful + `derived` | `command` must not contain `{agent_id}` |
| stateful | `resume_command` must contain `{agent_id}` |

`command` is still allowed to omit `{prompt}` / `{prompt_file}` — that is the
existing stdin-delivery path and stays supported.

### 5.2 Transcript parsing — new `raven/agent/subagent/backends/transcript.py`

Two parsers, both tolerant of non-JSON and malformed lines.

`parse_codex_jsonl(stdout) -> (thread_id, reply)` — ported from
`ui-webui/service/agentscope/subagent/_transcript.py`. The id is
`thread_id` of the first `thread.started`; the reply is `item.text` of the last
completed `agent_message` item.

`parse_claude_stream_json(stdout) -> (session_id, reply, is_error)` — new.
Verified empirically against `claude 2.1.220`:

```
{"type":"system","subtype":"hook_started",...,"session_id":"..."}   <- bulk noise
{"type":"system","subtype":"init","session_id":"...","tools":[...]}
{"type":"assistant","message":{...},"session_id":"..."}
{"type":"result","subtype":"success","is_error":false,"result":"OK","session_id":"..."}
```

The reply is `result` on the terminal `type == "result"` event; `is_error` comes
from the same event; `session_id` is read from any event as a fallback for a
derived create. A run that emits no `result` event yields `(id, None, False)`
and the backend falls back to raw stdout.

Parsing is not cosmetic here. The single "reply OK" run above produced roughly
40 KB of hook and init noise before the answer, so without extraction
`max_output_chars` (30000) truncates the real reply away entirely.

### 5.3 Instance registry — new `raven/agent/subagent/instances.py`

Persists handle -> CLI session id at `~/.raven/subagent_instances.json`.

```json
{
  "version": 1,
  "instances": [
    {
      "sessionKey": "web:abc123",
      "agent": "claude_code",
      "handle": "refactor-auth",
      "agentId": "11111111-2222-3333-4444-555555555555",
      "createdAtMs": 0,
      "updatedAtMs": 0
    }
  ]
}
```

A list, not a keyed object, so an arbitrary `handle` needs no key escaping;
lookups index in memory by `(session_key, agent, handle)`.

- Atomic write: temp file + `os.replace`, mirroring
  `update_subagents._write_atomic`.
- In-process dict cache, write-through, guarded by an `asyncio.Lock` so
  concurrent spawns cannot interleave a read-modify-write.
- Cross-process writes (gateway plus a CLI run) are last-writer-wins on the
  whole file. Acceptable for this data; noted rather than solved.

API: `lookup(session_key, agent, handle) -> str | None`,
`commit(session_key, agent, handle, agent_id)`,
`list_instances(session_key: str | None = None) -> list[dict]`,
`delete_session(session_key) -> int`.

**Lifetime is the chat session's, not a clock's.** There is no time-based
expiry: an instance stays resumable for as long as the conversation it belongs
to exists. Deleting the session deletes its records, which is also what bounds
the file's growth. The web UI has a single delete choke point
(`useSessions.remove`, `hooks/useSessions.ts:66-73`), so the cleanup call hangs
off that after the session delete succeeds, best-effort.

A session deleted outside the web UI leaves its records orphaned. That is
tolerable and deliberately not solved: keys are session-scoped so an orphan can
never collide with a live session, and every read path filters by the current
session key, so an orphan is invisible rather than wrong.

### 5.3.1 Handle and session id are different things

The instance `handle` is a registry key only. It is never substituted into a
command. The value bound to `{agent_id}` is either minted by raven
(`provisioned`) or read back out of the transcript (`derived`); the handle's
own shape is irrelevant to the CLI.

This matters because the handle falls back to `task_id` (8 hex chars) when the
caller omits `instance`, and CLIs constrain their session ids. Verified against
`claude 2.1.220`:

```
$ claude -p hi --session-id not-a-uuid-123 ...
Error: Invalid session ID. Must be a valid UUID.
```

A provisioned id is therefore always `str(uuid.uuid4())`, independent of the
handle. UUID is the strictest known constraint and is also a valid opaque token
for any looser CLI, so one minting strategy covers every `provisioned` agent
today; `derived` agents mint their own. A test asserts the provisioned id parses
as a UUID even when the handle is a non-UUID `task_id`, so the separation cannot
silently regress. If some future CLI needs a different shape, that is the point
to add an `idFormat` field — not before.

### 5.4 `CliAgentBackend` — `raven/agent/subagent/backends/cli_agent.py`

Adds the create/resume flow, following
`ui-webui/service/agentscope/subagent/_tool.py:277-464`.

```
stateful?
  no  -> agent_id = None, template = command            (today's behavior)
  yes -> handle = instance or task_id
         lookup(handle)
           hit  -> template = resume_command, agent_id = stored
           miss -> created = True
                   agent_id = uuid4() if provisioned else None

run the process

failure (non-zero exit, or is_error on a parsed transcript) -> raise
success:
  created and derived -> agent_id from the transcript parser
                         (claude session_id / codex thread_id / session_id_pattern group 1)
  created and agent_id -> commit(handle -> agent_id)     # deferred: only a successful create
  reply = claude result / codex agent_message / output_pattern group 1 / raw stdout+stderr
  derived create with no id -> append a "not resumable" warning last
```

Two details that must survive the port:

- **Deferred commit.** A failed create must not write the registry, or the
  handle is poisoned and every later call resumes a session that was never
  created.
- **Warning appended last**, after reply extraction, so `output_pattern` cannot
  swallow it.

`_build_argv` gains `{agent_id}` substitution and takes the template as an
argument so create and resume share one code path.

A third detail specific to `claude_stream_json`: **claude does not reliably exit
non-zero on failure under `-p`**, so the backend must treat `is_error: true` as
a failure independently of `returncode`.

### 5.5 Plumbing the handle to the model

- `SubagentBackend.run` (`backends/base.py`) gains
  `session_key: str | None = None, instance: str | None = None`.
  `RavenLoopBackend` and `OpenAIApiBackend` accept and ignore them.
- `SubagentManager.spawn` gains `instance: str | None = None`, carried in
  `origin` and forwarded to the backend. Existing callers
  (`sentinel/executor/spawn.py`, `action_executor.py`) are unaffected by the
  default.
- `SubagentManager.list_third_party_agents()` returns
  `list[tuple[str, str, bool]]` (name, description, stateful). The only
  consumers are `spawn.py:49` and one assertion in
  `tests/test_subagent_third_party.py`.
- `SpawnTool` exposes an `instance` parameter **only when at least one
  configured third-party agent is stateful**, described as: reuse the same
  handle to continue that session; omit for a fresh one.

### 5.6 Presets — `raven/agent/subagent/presets.py`

```
claude_code   kind cli, idSource provisioned, transcriptFormat claude_stream_json, timeout 600
  command  claude -p {prompt} --permission-mode auto --session-id {agent_id} \
           --output-format stream-json --verbose
  resume   claude -p {prompt} --permission-mode auto --resume {agent_id} \
           --output-format stream-json --verbose

codex         kind cli, idSource derived, transcriptFormat codex_jsonl, timeout 600
  command  codex -a never exec -s workspace-write \
           -c 'sandbox_workspace_write.network_access=true' --json {prompt}
  resume   codex -a never exec -s workspace-write \
           -c 'sandbox_workspace_write.network_access=true' --json resume {agent_id} {prompt}

mirothinker   kind openai, timeout 1200
  baseUrl  https://api.miromind.ai/v1
  model    mirothinker-1-7-deepresearch
  apiKey   ""
```

`--permission-mode auto` is a valid choice for the installed claude CLI.
`shlex.split` keeps `-c 'sandbox_workspace_write.network_access=true'` as two
tokens, which is what codex expects. Descriptions are ported from the RavenX
presets, adapted to Raven's `spawn(agent=..., instance=...)` wording.

### 5.7 RPC and service routes

`raven/web_rpc/methods_config.py` registers two new methods:

- `raven.subagents.instances` — optional `session_key` filter, returns
  `{"instances": [...]}` with the record shape from 5.3.
- `raven.subagents.instances.delete` — required `session_key`, drops every
  record for that session, returns `{"removed": <count>}`.

`ui-webui/service/raven_config_routes.py` adds `GET` and `DELETE` on
`/raven/subagents/instances` forwarding to them, alongside the existing
`list` / `presets` / `set`.

## 6. Frontend design

### 6.1 `/subagents` page — `src/pages/subagent/index.tsx`

Kept: the two-pane Sidebar-plus-form layout, `em-kicker` / `em-accent`
typography, the preset dropdown, the stateful badge, `DeleteDialog`, and the
empty states.

Changed:

- Data source `useSubagents` -> `ravenConfigApi.listSubagents / presets /
  setSubagents`.
- Primary key is `name`, not an id. Editing state stays
  `null` | `''` (new) | `<name>`. Create, edit, and delete all build the full
  list locally and PUT it. Renaming filters out the old name before appending.
- CLI form adds `resumeCommand`, `idSource`, `sessionIdPattern`,
  `outputPattern`, `transcriptFormat`.
- OpenAI form replaces the credential picker with inline `baseUrl` and a
  password `apiKey` field. **A blank `apiKey` on edit keeps the stored value**,
  backfilled from the loaded entry — the current `/raven-subagents` page wipes
  the key in exactly this case (`fromEntry` blanks it, `toEntry` sends
  `undefined`).
- Client-side validation mirrors the 5.1 validators, reusing the existing
  `cmdOk` / `resumeOk` live-hint pattern.
- Preset dropdown labels use `p.name`; Raven's presets RPC returns bare config
  dicts with no `preset_id` / `label`, and the RPC shape is not worth changing
  for a label.

### 6.2 Instance monitor — `src/components/subagent/`

`SubagentInstanceMonitor` takes `RavenThirdPartySubagent[]` instead of
`SubAgentView[]`, and sources its instance list from the registry:

- Fetch `GET /raven/subagents/instances`, filter to
  `sessionKey === 'web:' + sessionId`. That is exactly how the gateway agent
  builds it (`raven_gateway_agent.py:231`), and the loop's default is
  `f"{channel}:{chat_id}"` (`agent/loop/main.py:1223`).
- Refetch on mount and whenever the message count changes — a completed spawn
  is what mutates the registry, and that always coincides with a new turn. No
  polling timer.
- Exchange history still comes from `deriveInstances` over the transcript; the
  registry stores only handle -> agent_id, not conversation content. Registry
  entries with no transcript match render with zero exchanges, which is the
  point: they survive a page reload.
- `transportOf` reads the Raven config and gains a `claude` transport for
  `claude_stream_json`, alongside `codex` and `cli`.

### 6.2.1 Session deletion cleans up the registry

`useSessions.remove` (`src/hooks/useSessions.ts:66-73`) is the only place a chat
session is deleted. After `sessionApi.delete` resolves it fires
`DELETE /raven/subagents/instances?session_key=web:<sessionId>`, awaited but
error-swallowed: a gateway that is down must not block the session delete the
user asked for, and the orphan it leaves is invisible per 5.3.

### 6.3 Deletions

- `src/pages/raven-subagents/`, its route and import in `App.tsx`, and the
  sidebar entry in `AppSidebar.tsx:132-142`.
- `src/api/subagent.ts`, `src/hooks/useSubagents.ts`, and the `SubAgent*` types
  in `src/api/types.ts`. After 6.2, chat's only two consumers — the monitor and
  `subagentNameSet` (`ChatViewport.tsx:437`) — both read Raven config through a
  new `useRavenSubagents()` hook, so the AgentScope sub-agent surface leaves the
  frontend entirely.
- `src/api/ravenConfig.ts`: `RavenCliSubagent` gains the five new fields;
  `RavenOpenAISubagent` gains `temperature` / `maxTokens`.
- i18n: new `subagent-sidebar.*` keys for the added fields and
  `subagent-monitor.transport.claude`; the `credential*` keys drop out of this
  page (the credential page keeps its own).

## 7. Testing

Per AGENTS.md 5.4, extend the existing files rather than adding new ones.

- `tests/test_subagent_third_party.py` — schema validators (all four rules in
  5.1), both transcript parsers including malformed input, registry
  create/resume/deferred-commit/`delete_session`, `{agent_id}` substitution, the
  `is_error`-with-zero-exit case, and the 5.3.1 invariant that a provisioned id
  is a UUID regardless of the handle.
- `tests/test_web_rpc_config.py` — `raven.subagents.instances` with its
  `session_key` filter, and `raven.subagents.instances.delete`.
- Frontend gate: `pnpm -C frontend lint` (0 errors) and
  `pnpm -C frontend build`.

## 8. Known leftover

`raven/agent/subagent_dag/_store.py:47-49` still writes run directories under
`.ravenx_dag/`, a naming residue from the port. Unrelated to this change and
untouched here.
