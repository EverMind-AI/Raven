# Auto-generated instance handle for stateful sub-agents - design

Date: 2026-08-17
Status: designed
Base: `origin/main` at `558481e7`.

## Goal

Every call to a stateful sub-agent gets an addressable session handle, whether
or not the caller named one. `spawn` and `run_subagent_dag` mint one when
`instance` is omitted, and the handle reaches the main agent together with the
call's result, so a follow-up turn can continue that same conversation by
naming it.

## Why this is needed

An omitted `instance` always still produces *some* handle: the manager derives
`handle = instance or task_id` (`raven/agent/subagent/manager.py:337`) and each
transport derives the same thing again for itself
(`raven/agent/subagent/backends/cli_agent.py:368`,
`raven/acp_client/acp_agent.py:343`). A DAG node passes
`task_id=node.id`, so its unnamed handle is its own node id.

What the omission actually costs differs per transport, and none of the three
costs is visible to the caller.

**The built-in sub-agent and the OpenAI-compatible HTTP backend silently
forget.** Their only resume state is the message list in
`raven/agent/subagent/instance_state.py`, and both dispatch paths gate writing
it on the caller having named an instance (`manager.py:667`,
`raven/agent/subagent_dag/runner.py:539`). An agent the roster advertises as
stateful therefore persists nothing for an unnamed call. The registry still
records a row for it, so the instance appears on the UI strip beside a CLI
agent's row and offers the same direct-chat entry - which resumes for the CLI
agent and starts from empty for these two.

**The write side has no namespace.** An unnamed call commits its binding under
the bare handle. A DAG node with id `research` binds its session to handle
`research`, and a later call that explicitly names `instance="research"`
resumes a session it never asked to share. `CliAgentBackend._run_stateful`
documents this and states the fix is to namespace the write, not to tighten the
lookup.

**The ACP transport has no gate at all.** `AcpAgentBackend._open_session`
(`raven/acp_client/acp_agent.py:460`) looks up the bare handle
whenever the agent is stateful, without the `resumable` check the CLI transport
applies (`cli_agent.py:389`) and without `hold_handle`. An unnamed DAG node
reaching a hit there resumes a prior run's session, and two concurrent runs
with the same node id interleave on it.

A minted, globally unique handle removes all three: the message list is written
because the call now looks named, the write lands on a key no explicit name can
collide with, and the ACP lookup misses by construction.

## Decisions taken during design

| Question | Decision |
|---|---|
| Purpose of minting | Addressability - every stateful call becomes continuable, so `resumable` flips to true for them |
| Handle form | Semantic prefix plus a short unique suffix; must not collide across runs or sessions |
| Growth of `messages.json` | Accepted. Recorded below as a known cost with a follow-up, not designed around |
| Where the handle is told to the model | Completion announcement and DAG run summary; deliberately **not** the immediate `spawn` return |
| Config flag | None. Rollback is a revert |

The immediate `spawn` return is excluded on purpose: at that moment the model
has no result to act on, the string stays in context for the rest of the
session, and the handle is only actionable once the call finishes.

## Approach: mint at the tool layer

Minting happens in `SpawnTool.execute` and `SubAgentDagTool.execute`, before
either dispatches. The value is placed in the same `instance` field the model
would have filled, so everything downstream is indistinguishable from a call
that named its instance. No dispatch-path logic changes anywhere:
`resumable=True`, `hold_handle`, the `instance_state` gates and the
`manifest.instance` field all become correct as a consequence rather than by
separate edits. The manager and runner edits listed below are the announcement
text and the audit bit, not gates.

### `mint_handle`

Lives in `raven/agent/subagent/instances.py`, the module that owns the handle
key space.

```
mint_handle(seed: str) -> str
```

Returns `f"{slug}-{uuid4().hex[:6]}"`.

- `slug` is `seed` lowercased, with every run of characters outside `[a-z0-9]`
  folded to a single `-`, stripped of leading and trailing `-`, and truncated
  to 32 characters. An empty result falls back to `agent`.
- The six hex characters (24 bits) make a collision on the same slug across
  runs, sessions and graphs negligible at any realistic handle count, not
  impossible, and put it outside the space of names a model would choose by
  hand, so a minted handle can never shadow an explicit one.
- No path-escaping duty here: `instance_state_path` already routes the handle
  through `safe_path_segment` (`raven/utils/helpers.py:202`), and the slug
  rules above are strictly narrower than what that accepts.
- `DagNodeSpec.instance` carries no pattern constraint
  (`raven/agent/subagent_dag/_graph.py:59`), so a minted value needs no
  additional validation to survive a re-parse.

### `spawn`

In `SpawnTool.execute` (`raven/agent/tools/spawn.py:155`), after the existing
`_reject_useless_instance` check and before `self._manager.spawn(...)`: when
`instance` is empty and the target agent is stateful, set
`instance = mint_handle(label or task)`. `mint_handle` truncates internally, so
the caller hands over the whole string.

The stateful predicate is the **same roster lookup**
`_reject_useless_instance` (`spawn.py:127`) already performs, factored into one
helper so the rejection gate and the minting gate can never disagree.
`agent=None` - the built-in in-process sub-agent - counts as stateful; that
method's own comment already establishes it is resumable per handle.

### `run_subagent_dag`

In `SubAgentDagTool.execute` (`raven/agent/subagent_dag/tool.py:569`), after
`run_id = make_run_id()` (`tool.py:617`) and before the run starts in either
mode: for each node whose sub-agent has `AgentCapabilities.stateful` and whose
`instance` is empty, set `instance = mint_handle(node.id)`.

`DagNodeSpec` is a pydantic model, so the spec is rebuilt with
`model_copy(update=...)` rather than mutated. Minting after validation keeps a
rejected graph costing zero mints, and minting before `self._run(...)` covers
the foreground and background paths from one place.

Nodes that already share an explicit `instance` are untouched, so the runner's
sequential grouping for a shared handle (`runner.py:270`) keeps working; minted
handles are unique per node, so those nodes stay parallel.

## What the UI needs

The web instance strip derives its rows from **the model's tool-call input**,
not from the registry:
`ui-webui/frontend/src/components/subagent/deriveInstances.ts:127` reads
`call.input.instance` and skips the call entirely when it is absent
(`if (!handle) continue`, line 128). Minting inside `execute` happens after
that input is recorded, so without a further change the strip would not see a
minted handle at all.

The handle travels back over the existing metadata channel instead.
`Tool.take_metadata` (`raven/agent/tools/base.py:135`) is the opt-in payload a
tool hands to the turn stream for a UI rather than for the model.

- `SpawnTool` implements `take_metadata`, returning
  `{"instance": handle, "instance_auto": true}`.
- `deriveInstances` falls back to `results.get(call.id).metadata.instance` when
  `input.instance` is absent. That block already reads the same metadata object
  for `agent_id`, so this extends an existing read rather than adding a
  channel.
- For DAG nodes the handle does not travel back through `take_metadata` the
  way `spawn`'s does. The handles are already in `result.files` and in the
  `dag_run_completed` event's manifest (`tool.py:751`), because `_finalize`
  writes `node.instance` there (`runner.py:627`, `runner.py:640`); the
  frontend's node loop (`deriveInstances.ts:154`) instead falls back to the
  live DAG overlay (`DagRunLive.nodes[].instance`, from `DagRunsContext`),
  matched by node id within the call's run id. `restoreDagRuns.ts` rebuilds
  that overlay from the durable run manifest, so the fallback also survives a
  reload, not just a live run.

**User-visible consequence:** the instance strip gets noticeably denser. A
`spawn` without an instance contributes no row today; it will contribute one
per call, marked `stateful: true` and therefore carrying a direct-chat entry.
That is what addressability means here, but it is a UI change and not a
backend-only one.

## Model-facing text

**Completion announcement** (`raven/agent/subagent/manager.py:726`) gains one
line before `Result:`, only when the handle is addressable:

```
Instance handle: <handle> -- pass it as spawn's `instance` to continue this same conversation.
```

That announcement currently closes with *Do not mention technical details like
"subagent" or task IDs* (`manager.py:755`). Left as is, the model would
classify the handle it was just given as a forbidden technical detail and drop
it, cancelling the change. The sentence is reworded to constrain only what the
model says **to the user**, leaving what it may use on a later call untouched.

**DAG run summary** (`raven/agent/subagent_dag/tool.py:772`) annotates each
stateful node's line:

```
- research [completed] (instance: research-a3f9): /path/research.out.md
```

**Tool schema** (`raven/agent/tools/spawn.py:111`) currently tells the model
*omit it to start a fresh one*. That becomes false once omission mints a
handle. It is rewritten to say that omitting the field assigns one
automatically and reports it when the call finishes. Missing this edit leaves
the model believing an unnamed call cannot be continued, which would waste the
rest of the change.

## Audit bit

`instance` stays a single field holding the effective handle; the fact that it
was minted rather than chosen is recorded beside it, as `instance_auto: true`
in `SpawnRecord.meta` and in the DAG manifest entry (`runner.py:627`,
`runner.py:640`). This keeps every downstream consumer reading one field while
preserving the distinction for anyone auditing later.

## Interaction with unmerged work

The sub-agent everos memory record (branch
`feat/subagent_everos_memory_record`, not on `main` at the time of writing)
resolves a call's everos session by looking its handle up in the registry, on
both the spawn and the DAG path. Minting changes neither derivation and
improves what they find: the handle a record is keyed by becomes a readable
name rather than an opaque task id. Whichever branch lands second rebases onto
the other; the two touch different lines of `manager.py` and `runner.py`, so no
semantic conflict is expected.

## Non-goals

- **Reclaiming `messages.json`.** Nothing reclaims the direct-chat tree today;
  `instance_state.py` states that adding it would also start deleting the
  pre-existing `spawn/` and `mas_dag/` audit trees, and defers it. Minting
  makes that growth apply to every stateful call rather than only named ones.
  Accepted as a known cost; see follow-ups.
- **Repairing the ACP gate.** Unique handles make the ungated lookup at
  `acp_agent.py:460` unreachable in practice, which is mitigation, not repair:
  the missing `resumable` check and the missing `hold_handle` are still absent
  from that code. Left as a follow-up so this change stays one idea.
- **Changing stateless behaviour.** A stateless agent still gets no minted
  handle, and `_reject_useless_instance` still refuses an explicit one.
- **A config flag.** Rollback is reverting the branch. A boolean would remain
  in the schema forever and would not undo handles already written to the
  registry, so it would not actually restore the prior state.

## Files touched

| File | Change |
|---|---|
| `raven/agent/subagent/instances.py` | Add `mint_handle` |
| `raven/agent/tools/spawn.py` | Mint when omitted; factor the stateful predicate; implement `take_metadata`; rewrite the `instance` schema description |
| `raven/agent/subagent_dag/tool.py` | Mint per stateful node after validation; annotate summary lines; reword the node schema's `instance` description |
| `raven/agent/subagent/manager.py` | Handle line in the announcement; reword the closing instruction; `instance_auto` in `SpawnRecord.meta` |
| `raven/agent/subagent_dag/runner.py` | `instance_auto` in the manifest entry |
| `ui-webui/frontend/src/components/subagent/deriveInstances.ts` | Metadata fallback for spawn rows; live-overlay fallback for DAG node rows |
| `raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md` | Correct the "always starts from a clean session" line |

## Testing

Existing files only, per AGENTS.md section 5.1.

- `tests/test_subagent_third_party.py` - a stateful agent called without
  `instance` reaches `manager.spawn` with a non-empty `slug-hex` value; a
  stateless agent still reaches it with `None`; an explicit instance is passed
  through unchanged and never overwritten.
- `tests/test_subagent_manager.py` - under a minted handle the `instance_state`
  gate opens and `messages.json` is written; the announcement carries the
  handle line for a stateful agent and omits it for a stateless one.
- `tests/test_subagent_dag_runner.py` - this file covers both `run_dag` and
  `SubAgentDagTool`, and minting happens in the tool, so all DAG-side cases go
  here: stateful nodes get an instance while stateless nodes keep `None`;
  **the same spec submitted twice yields different handles**, which is the
  regression lock on cross-run collision; `instance` and `instance_auto` reach
  the manifest; nodes sharing an explicit handle still serialize.
- Frontend: `ui-webui/frontend` has no test runner and
  `subagent/deriveInstances.ts` has no existing test, so the fallback is
  verified by hand - run one `spawn` without `instance` against a stateful
  agent and confirm a row appears on the strip with the minted handle. This is
  a manual verification step, not an automated one.

## Risks and rollback

Three user-visible behaviour changes: every stateful call becomes a resumable
session; the built-in and HTTP backends write a message list on every call
instead of only named ones; the instance strip gets denser and offers direct
chat on rows that previously did not exist.

Rollback is `git revert` of the branch. Handles already minted stay in the
registry and in already-written manifests; they remain valid keys, and a
reverted build simply stops producing new ones.

## Follow-ups

1. Add the `resumable` gate and `hold_handle` to `AcpAgentBackend._open_session`
   so the ACP transport is repaired rather than merely bypassed.
2. Teach session deletion to reclaim the direct-chat tree, which requires first
   deciding what happens to the `spawn/` and `mas_dag/` audit trees beside it.
3. Consider a retention bound on minted handles' message lists if the growth
   turns out to matter in practice.
