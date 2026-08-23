# User-created sub-agent instances in the TUI - design

Date: 2026-08-21
Status: designed
Base: `origin/main` at `51fe028a`.

## Goal

A `/new-instance` slash command in the TUI opens an overlay picker of the
direct-chattable sub-agents, creates a fresh instance of the one selected, and
switches into a direct chat with it. The main agent learns that the user did
this, through the direct-chat handoff block it already reads.

## Why this is needed

Direct chat exists and works, but only against instances that already exist,
and every path that creates one runs *for* the main agent: `spawn` mints a
handle for a delegated task, a DAG node binds one per graph node. So the user
can only direct-chat a sub-agent that Raven happened to delegate to first.

`/instance` (`ui-tui/src/app/slash/commands/core.ts:203`) lists what exists and
switches between them. There is nothing that starts one, so "talk to
Raven-PPT" is only reachable by first asking Raven to give Raven-PPT some task,
which puts an unwanted turn in both conversations.

## What the code already supports

`SubagentManager.chat` does not require the instance to pre-exist: it validates
the *agent* and writes the registry row itself on the first turn
(`raven/agent/subagent/manager.py`). So "create an instance" is not a new
lifecycle - it is minting a handle and writing the row that a first turn would
have written anyway.

`InstanceRow.status` is a free-form string, and `reconcile_instance_rows`
rewrites only `pending` and `running`. A new status therefore passes through
untouched, including across a gateway restart.

## Decisions

### D1 - An overlay picker, not a numbered transcript listing

`/instance` prints a numbered list and takes an index. `/new-instance` opens an
overlay instead (`↑/↓ · Enter · 1-9 · Esc/q`), the vocabulary the session and
model pickers already use. `overlayControls.tsx` supplies `useOverlayKeys`,
`OverlayHint` and `windowOffset`, and `sessionPicker.tsx` is a working template
for the rest.

The picker is where the constraint in D2 becomes visible: an agent that cannot
be direct-chatted should not be offered and then refused.

### D2 - Only stateful, enabled agents are offered

`chat` refuses a stateless agent outright - against one, every turn starts a
fresh conversation, so the chat on screen would read as the instance forgetting
(`manager.py`, `declared_stateful`). The picker filters to `enabled &&
stateful` so the refusal is never reachable from it.

This requires a `stateful` flag on the `subagents.list` row, which the TUI
currently has no way to derive: it is `agent_meta(cfg).stateful`, the same
derivation the roster and the DAG pre-check use. Declared optional on
`SubagentRow`, matching how `vendored` and `building` are declared, so a client
talking to an older server degrades rather than fails.

### D3 - Creation is its own announceable event

The handoff block is built from turns, appended as each one lands. A creation
has no turn, so it needs its own entry kind or it is invisible.

It gets one. Creating an instance and saying nothing to it still produces a
handoff entry on the next main-agent turn, and still bumps the pending count
that drives the `N` hint. This is the case worth reporting: Raven knowing that
an unused Raven-PPT instance is sitting there is actionable in a way that
annotating a turn it can already see is not.

Consequence: `pending_handoff_count` now counts turns *and* creations. Its
description is corrected in `raven/rpc/models.py` and `rpc-schema/openrpc.json`
rather than left to quietly mean something new.

### D4 - The handle is minted, not typed

Enter creates; there is no name field. `mint_handle` is what the auto-instance
handle path already uses, so a collision is structurally impossible.

This also keeps a load-bearing comment true. `DirectChatHandoff`'s docstring
states the block is prepended to the user's prompt unwrapped, and that this is
safe "only because every byte here is raven-minted, never sub-agent output:
agent names come from config, handles from the registry". A user-typed handle
would not actually break the threat model - the user's own text is already
unwrapped in that same prompt - but it would falsify the invariant as written.
Renaming is a clean follow-up if minted handles prove hard to tell apart.

## Design

### Backend: `SubagentManager.create_instance`

```
validate  -> the two checks chat() makes: the registry row exists and is
             enabled; declared_stateful(agent)
handle    -> mint_handle(agent)
persist   -> registry.upsert_spawn(session_key, agent, handle, "idle")
return    -> InstanceCreation(agent, handle, created_at_ms)
```

It writes exactly one thing: the registry row. No record directory -
`DirectChatRecord.open` makes those per turn, and there is no turn yet. That is
what makes a creation-only handoff entry name no path (see below).

It lives on the manager, not in the RPC handler, because the enabled/stateful
validation already lives there. A second copy one layer up is exactly the drift
`AgentRegistry` exists to prevent.

The registry write is `upsert_spawn` directly rather than the manager's
`_write_spawn_status` helper. That helper swallows every failure including a
timeout, which is right for a status write beside a task that runs regardless,
and wrong here: the row *is* the deliverable, and reporting a creation that did
not persist gives the user a chip that is not there. `upsert_spawn` already
tolerates a failed flush on its own (the in-process record still lands), so
only a genuine failure propagates, and it reaches the picker as an error.

**Status `idle`.** New to Python, already in the web UI's `nodeStatus`
catalogue (its `idle` key, which carries an English and a Chinese label),
emitted by nothing today, so no i18n work.
Two existing behaviours are correct for it by construction: `reconcile_instance_rows`
leaves it alone across a restart instead of turning it `interrupted`, and the
TUI chip strip counts only `running`/`pending` as running, so no spinner.
`upsert_spawn`'s docstring, which enumerates the statuses, is updated.

### The announcement

`DirectChatHandoff._pending` becomes one ordered list of a tagged union - turn
or creation - so both group under a single `(agent, handle)` heading and their
order survives. `record_created()` appends a creation; `pending_count` counts
both.

```
[subagent direct chat activity since your last turn]
Raven-PPT / raven-ppt-a1b2c3
  created by the user at 2026-08-21T09:00:00Z, no turns yet
Raven-Code / raven-code-9f8e7d
  created by the user at 2026-08-21T09:02:00Z
  2 turns, 2026-08-21T09:03:00Z -> 2026-08-21T09:05:10Z
  /root/.raven/sessions/<sid>/subagents/direct/Raven-Code/raven-code-9f8e7d/
    <call_id>/{prompt.md,out.md}
```

A creation-only group names no path, because none exists yet. Pointing the main
agent at a directory that is not there is the failure the existing
`(record unavailable)` branch already guards against for turns.

The header widens from `direct chats` to `direct chat activity`: the block can
now report a creation with no chat in it, and the old wording makes that entry
read as a contradiction to the model reading it.

### RPC contract

`subagents.instance.create` -> params `{session_key, agent}`, result
`{instance: InstanceRow}`.

| File | Change |
|---|---|
| `raven/rpc/methods/instances.py` | handler + registration |
| `raven/rpc/models.py` | params/result models, `METHODS` entry, corrected `pending_handoff_count` description |
| `rpc-schema/openrpc.json` | method entry, `stateful` on `SubagentRow`, same description fix |
| `raven/rpc/methods/subagents.py` | `_rows` emits `stateful` |
| `ui-tui/src/rpc/generated.ts`, `ui/src/rpc/generated.ts` | regenerated, never hand-edited |

The handler records the creation on the handoff. `instances_list` in that same
module already reaches `loop._direct_handoff` for the count, and the module
describes itself as "the things the direct-chat surface addresses" - a creation
is a client action, unlike a turn, which the loop runs. So the RPC layer is
where it belongs, and `main.py` does not grow.

### TUI

`components/newInstancePicker.tsx`, modeled on `sessionPicker.tsx` minus its
delete-confirm flow, reusing `useOverlayKeys` / `OverlayHint` / `windowOffset`.

- Loads `subagents.list` with `probe: false` - instant open; readiness
  diagnosis is `/subagents`' job, and a broken agent errors clearly on its
  first send.
- Filters `enabled && stateful`, shows name, kind, description, and how many
  instances of that agent already exist (read from `$directChat.instances`, no
  extra call - it is the thing you want to know before opening a fifth one).
- On Enter: create, patch the returned row into `$directChat.instances` so the
  chip appears immediately, `enterDirect`, close. `scheduleInstanceRefresh`
  reconciles behind it. Errors render inline the way `sessionPicker` does.

Entering triggers the existing history load, which returns `{turns: []}` for a
fresh instance with no code change - the record-dir read already swallows
`OSError`.

Wiring: `newInstance: boolean` on `OverlayState`, added to `buildOverlayState`,
`$isBlocked` and `resetFlowOverlays` (it is user-toggled, so it must survive a
turn ending), plus a `FloatBox` branch in `FloatingOverlays`.

The slash command sits beside `/instance` in `core.ts`. No argument opens the
picker; an optional agent name skips it (`/new-instance Raven-PPT`), which is
the same create call and a few lines.

## Out of scope

- Web UI parity (`raven.subagents.instances.create` plus a button). The backend
  is shared, so it is a small follow-up.
- Renaming a handle, if minted ones prove hard to tell apart (D4).
- Deleting an instance from the TUI. `subagents.instance.forget` exists on the
  RPC surface but no TUI surface calls it, so instances accumulate in the
  registry once creating one is this cheap. Worth a follow-up; not this change.
