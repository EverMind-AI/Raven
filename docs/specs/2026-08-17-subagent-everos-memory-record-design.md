# Sub-agent everos memory record - design

Date: 2026-08-17
Status: designed
Base: `origin/main` at `e9c19efb`.

## Goal

After a sub-agent finishes a call, record what that sub-agent wrote into
everos as a small file next to the call's own prompt and output, on all three
delegation paths (`spawn`, `run_subagent_dag`, direct chat).

The file's reader is **another sub-agent**, not a human auditor. It answers one
question: what did this sub-agent do? Everything that does not serve that
question is a diagnostic and belongs in the log.

## Why this is needed

A sub-agent that runs on the everos memory backend writes episodes and cases
into a store the host never sees. The host holds the call's prompt and output;
everos holds what the sub-agent concluded from that call. Nothing joins them,
so the reasoning a sub-agent distilled is invisible to everything downstream of
it - including the next sub-agent that would benefit most from it.

## What makes this cheap

The host already knows the exact everos join key.

All three Raven forks pass the host-substituted `{agent_id}` through to their
own Raven as `--session cli:{conversation}`
(`subagents/raven-code/run.py:430`, `subagents/raven-oncall/run.py:520`,
`subagents/raven-research/run.py:303`), and that session id lands verbatim on
every memory everos extracts from the call. The host mints that `agent_id`
itself and persists it in `InstanceRegistry` keyed by
`(session_key, agent, handle)` (`raven/agent/subagent/backends/cli_agent.py:549`,
`raven/agent/subagent/instances.py:72`).

everos exposes `POST /api/v2/memory/get`, which takes `user_id` XOR `agent_id`,
a `memory_type`, and a `filters` DSL that accepts `session_id`. Verified
against the running service (everos 1.2.1) during design:

```
{"user_id":"raven-code","memory_type":"episode",
 "filters":{"session_id":"cli:9616b4a0-9ae8-4961-a793-f047d9488e68"}}
-> count 1
```

So the record needs no proxy, no change to any fork, and no time-window
diffing against a global store. It is one filtered read per call.

## Non-goals

- **Causal proof.** The join key is per *instance*, not per call: repeated
  calls to one handle share `cli:<agent_id>`, and everos extracts memories
  asynchronously, so a memory observed during call N may summarize call N-1.
  The record says these memories appeared for this instance around this call.
  It does not claim to have caused them.
- **Delivering the record's *content*.** Nothing injects a record's text into
  another sub-agent's prompt. Section 7 tells a DAG node the *paths* of its
  upstream records; reading them is the node's own business, through its own
  file tool. A content-bearing placeholder (`{{ node.memory }}` beside the
  existing `{{ node.output }}`) remains deferred.
- **Recording the host's own memory writes.** Only sub-agents.

## Scope

Every delegation path, because the question "what did this sub-agent do" is
the same question on all three:

| Path | Record directory | File |
|---|---|---|
| `spawn` | `subagents/spawn/<call_id>/` | `memory.json` |
| DAG | `subagents/mas_dag/<run_id>/` | `<node_id>.memory.json` |
| Direct chat | `subagents/direct/<agent>/<handle>/<call_id>/` | `memory.json` |

The DAG filename mirrors `<node_id>.out.md`, so a run directory keeps one
naming rule.

## Design

### 1. Config interface

The identity is declared, not discovered. Add an optional block to
`ThirdPartyCliSubagentConfig` (`raven/config/schema.py:912`) - `cli` is the only
kind that runs a Raven fork, so it is the only kind that can write everos:

```python
class SubagentEverosConfig(Base):
    user_id: str | None = None       # owns episode
    agent_id: str | None = None      # owns agent_case
    base_url: str | None = None      # defaults to the host's everos base_url
    session_prefix: str = "cli:"     # fork-side convention, see below
```

A validator rejects a block with neither `user_id` nor `agent_id` - it could
query nothing, so accepting it would only produce silent empty records.

Declaring the block **is** the opt-in. An agent without one gets no trace, no
file, and no HTTP call.

The identity is read off the agent table's row (`AgentRegistry.get(name).config`)
each time a record is scheduled, not cached in a map of its own. The row already
holds the config the block is declared in, so a hot `apply_agents` cannot leave
the two disagreeing -- which a separately-maintained map could. That is what keeps the ACP agents (`Coder`, `Writer`,
`opencode`), which never touch everos, from accumulating empty files.

`base_url` defaults to the host's `plugins.config["everos-memory"].base_url`
(`raven/config/update.py:299`) because in practice every fork points at the same
local service, but stays overridable because nothing guarantees that.

`session_prefix` is configurable rather than hardcoded for the same reason the
identity is declared rather than read out of the fork's own `config.json`:
`cli:` is a convention living in each fork's `run.py`, and burning it into the
host coupling would reintroduce exactly the dependency this block exists to
avoid.

### 2. Join key

```
session_id = session_prefix + InstanceRegistry.lookup(session_key, agent, handle)
```

No registry entry means no key - a stateless agent, or an `id_source="derived"`
agent whose id was never read back. That case writes no file and logs at debug.
It is a normal outcome, not an error.

### 3. Recorder

New module `raven/agent/subagent_memory.py`.

```python
await record_memories(agent=..., identity=..., resolve_session_id=..., write=...)
```

A single post-finish call, not an `open`/`settle` pair. An earlier revision of
this section had a `MemoryTrace.open(...)` before dispatch, which existed to
capture the call's start time for the record's `window` field. Section 4 below
dropped `window`, so `open` would capture nothing and the pair collapses.

The join key is therefore resolved at record time rather than before dispatch,
which it has to be regardless: `InstanceRegistry` only holds the instance's id
once the backend commits it (`cli_agent.py:610`), so a first call to a fresh
handle has no registry row until the run is over.

It is scheduled after the call's own `record.finish(...)`, never awaited
by the dispatch path: everos extraction runs an LLM, and a sub-agent's reply
must not wait on the host's bookkeeping. It polls
`POST /api/v2/memory/get` with backoff (2s, 4s, 8s, 16s, 30s, 30s...) until
either the result stops growing for one interval or the budget is spent.

The task goes in an index of its own on the manager, which the cancellation
sweeps reach so a closing session is never held open by a poller, and which
`has_active` ignores. Deliberately not `_track`: that indexes into
`_session_tasks`, whose one reader refuses a working-directory change while a
session has work in flight -- and a poller is not work the user is waiting on.
A cancelled call schedules no record at all, on any path: a record scheduled
from a `finally` running inside a cancel sweep is created after that sweep
snapshotted what to reap, so nothing could ever reap it.

**Assumption to confirm in review:** the budget defaults to 60s. This was
raised during design and left unanswered. 60s covers the observed extraction
latency for short calls; a long node whose extraction outlives it lands
`status: "pending"`, which is a truthful terminal state rather than a wrong
one. The value is a module constant (`_DEFAULT_BUDGET_S` in
`subagent_memory.py`) today: no config field exists and no production caller
passes `budget_s`, so raising it is a code change.

### 4. Record file

```json
{
  "agent": "Raven-Code",
  "instance": "audit-a3f9c1",
  "status": "settled",
  "memories": [
    {"type": "episode", "text": "..."},
    {"type": "agent_case", "text": "..."}
  ]
}
```

- `instance` is the call's instance handle, present only when the call had one.
  It is the one piece of identity that earns a place in the record: passing it
  back as `spawn`'s `instance` continues that same conversation, so it is
  actionable for the reader in a way the everos identity and session id are not.
  Every stateful sub-agent now has one, minted when the caller named none, so in
  practice only a stateless call omits it. Omitted rather than nulled: a key
  that is always present but usually empty costs every reader a check and tells
  it nothing.

- `text` is everos's own complete account, joined with ` - `: an episode's
  `subject` then its `episode`; a case's `task_intent`, `approach` and
  `key_insight`. Uncapped.

  An earlier revision recorded an episode's `summary` alone, capped at 500
  characters, on the premise that `summary` was everos's compressed form. It is
  not: measured against live everos 1.2.1, `summary` is a literal 200-character
  prefix of `episode`, cut mid-word, so the record opened a sentence it never
  finished. `approach` was missed the same way -- it is a case's *how*, which is
  most of what "what did this sub-agent do" is asking. `summary` survives only
  as the fallback when `episode` is absent.

  Uncapped because the reader is a sub-agent whose file tool already handles
  length; a cap here would drop the end of the narrative, which is where the
  findings live.
- `status` is `settled` | `pending` | `unavailable`. It stays because without
  it an empty `memories` array is ambiguous:
  - `settled` - everos returned memories and the result stopped growing.
  - `pending` - nothing was found within the poll budget. Deliberately does
    *not* claim which of the two causes applies. An earlier revision of this
    section split them, promising `settled` + empty for "the sub-agent wrote
    nothing" and `pending` for "extraction had not finished". That distinction
    is not observable: everos holding nothing and everos not having extracted
    yet are identical from outside, so the split promised a reading the system
    cannot compute.
  - `unavailable` - everos could not be reached.
- Only `episode` and `agent_case`. `profile` and `agent_skill` accumulate
  across calls - they describe what a sub-agent *is*, not what it just did, and
  including them would dilute the signal the reader came for.

Identity, base URL, session id, time window, per-type counts, item ids, and
timestamps are all deliberately absent. They are diagnostics; they go to the
log, where a human debugging the join can still find them, and where they do
not cost the reader context.

A memory extracted late enough to be observed by two consecutive calls appears
in both files. No flag marks it. In a text-shaped reading surface a repeated
line is harmless, and a field explaining it would cost every reader something
to serve a rare case.

### 5. Wiring

Three sites, each scheduling the record where the path already finishes. On the
two manager paths a `finally` covers every outcome branch at once, so each is a
single addition rather than one per branch:

| Path | Site |
|---|---|
| spawn | `raven/agent/subagent/manager.py`, the `try` whose branches call `record.finish` (lines 668-684) |
| direct chat | `manager.py`, the `try` at line 435 (three `finish` branches) |
| DAG | `subagent_dag/runner.py:_run_node`, after `_write_node_status` at line 545 |

### 6. Failure behaviour

Every failure mode is absorbed by the recorder and none reaches the call:

| Condition | Result |
|---|---|
| No everos block declared | No trace, no file, no HTTP call |
| No registry entry (no join key) | No file, debug log |
| everos unreachable or erroring | One `unavailable` record, warning log, no retry into the run |
| Budget spent with nothing found | `pending` record |
| Any exception inside the recorder | Swallowed and logged |

This matches the contract the three existing record classes already state:
history is an audit trail, and losing it must never take down the call it
describes.

### 7. Telling a DAG node where its upstream records are

Every DAG node whose sub-agent can open local paths gets a block appended to
its rendered prompt, naming each upstream node and the absolute path of that
node's record:

```
## Upstream memory records

- audit: <run_dir>/audit.memory.json
- research: <run_dir>/research.memory.json

These are written asynchronously after a node finishes, so a file may not exist
yet, or may carry '"status": "pending"'. Either way that upstream has no
distilled memory available yet: proceed without it rather than waiting for it or
treating its absence as an error.
```

Four decisions, each with its reason:

- **Appended automatically, not requested by a placeholder.** A node author does
  not have to know the feature exists. The cost is that every node with upstream
  carries the block, including nodes that will never care.
- **Transitive upstream, not just direct dependencies.** A node reads what
  everything before it concluded, not only its immediate predecessor. In a deep
  graph the list grows accordingly.
- **Gated on `reads_local_files`.** The same capability that already refuses
  path placeholders (`_check_path_placeholders`) suppresses this block entirely:
  a path handed to an agent running elsewhere is meaningless text. An agent
  absent from the capability map is *not* gated, matching what that existing
  check does with an unknown agent.
- **Paths, with no existence guarantee.** The record is written by a
  fire-and-forget poller after the node finishes (section 3), while the runner
  starts the next wave as soon as its dependencies complete. So a listed file
  usually does *not* exist yet when the downstream node starts. Measured: the
  poller's first look lands at t=0 and finds nothing; settling takes seconds to
  the full budget. The block therefore tells the node what to do about absence
  rather than merely warning of it. The alternatives -- making the downstream
  wait, or writing a `pending` placeholder file up front -- were both considered
  and declined, the first because it reintroduces the blocking section 3 exists
  to avoid.

Paths are built by `memory_path_in` (`subagent_dag/_store.py`), the module-level
helper `DagRunStore.memory_path` delegates to, so an upstream belonging to an
earlier run of the same session is named under *its* run id via
`SessionNodes.owner`.

## Testing

- New `tests/test_subagent_memory.py` covering the recorder against a stubbed
  everos: no declared identity writes nothing; a found episode and case produce
  the two-field shape; a spent budget writes `pending`; an unreachable service
  writes `unavailable`; text is uncapped; `profile` and `agent_skill`
  are not queried.
- One "the file lands" case added to each existing path test.
  `tests/test_subagent_history.py` already covers both the spawn and the direct
  record classes, so both record-shape cases go there;
  `tests/test_subagent_dag_runner.py` takes the DAG one, and
  `tests/test_subagent_direct_chat.py` takes the manager-level direct-chat flow.

Per AGENTS.md section 5.1 these extend the existing files rather than adding
per-feature ones.

## Domain terms

`memory record` is a new term (the per-call file) and `memory trace` is a new
term (the recorder object that produces it). Per AGENTS.md section 6 both get
defined in `CONTEXT.md` in the same change, alongside the existing sub-agent
history vocabulary.

## Risks

- **Attribution is approximate**, as stated in the non-goals. A reader that
  treats the record as proof of what one call caused will occasionally be
  wrong. The fix if this bites is a content-bearing placeholder carrying the
  window explicitly, not a field in this file.
- **Poller load.** One background task per call with a declared identity, each
  making a handful of HTTP calls to a local service. Bounded by the existing
  dispatch concurrency cap.
- **Rollback** is deleting the config block: with no identity declared the
  entire feature is inert.
