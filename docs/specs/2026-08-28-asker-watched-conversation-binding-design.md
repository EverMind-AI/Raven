# asker binding for a watched SUBAGENT relay turn - design (v0.2.0 refactor)

Date: 2026-08-28
Status: proposed
Base: `refactor/raven_v0_2_0`

## Goal

Open the turn's `Asker` for a `SUBAGENT`-origin relay turn when its
conversation has a live watcher, so a sub-agent question spawned from that
turn reaches the person watching instead of being declined in milliseconds by
a silent branch. The gate's proxy changes from "turn origin" alone to "turn
origin + is anyone watching this conversation", using the subscription
registry that already exists.

## Why this is needed

### The incident (2026-08-28)

1. 07:27 - a USER turn orchestrates a DAG. Sub-agent questions inside this
   turn pass through; the round trip works (dr clarify answered).
2. 08:15 - a DAG node dies on its own (timeout).
3. 08:20 - the TUI was restarted; no message was sent in the session after
   the restart. The DAG announce fires a relay turn through
   `SubagentManager._inject` (`raven/agent/subagent/manager.py:1520-1541`):
   `origin=Origin.SUBAGENT`, `conversation=origin["session_key"]`. The main
   agent in this turn spawns raven-code.
4. 08:23 - raven-code's write gate asks; answered empty in 39ms; WRITE
   BLOCKED.
5. 08:25 - raven-code's model calls `ask_user` itself ("授权写入,继续产出
   HTML / 暂不写入"); answered in 5ms with no answer. The instance's whole
   question channel is dead - same tooling the dr scenario rides on.
6. The main agent notices the unanswered authorization and works around it by
   having the sub-agent emit the HTML as text, written to disk by the main
   agent.

### The root cause

Both turn runners bind the asker only for USER turns, with an identical gate
(`raven/rpc/spine.py:185`, `raven/gateway/spine.py:90`):

```python
interactive = req.origin is Origin.USER and isinstance(ask_tool, SupportsDirectAsk)
start_ask_turn(AskViaTool(ask_tool) if interactive else None, ...)
```

`Elicitor` and `AskUserResponder` read `current_ask()` at construction, in the
run's own context (`raven/acp_client/elicitor.py:70`,
`raven/acp_client/acp_agent.py`). A relay turn runs with
`origin=SUBAGENT`, so every sub-agent spawned from it captures
`asker=None`, and every question for the whole run is declined:

- elicitation route: `_elicit` returns `elicitation.decline()` on
  `asker is None` (`raven/acp_client/elicitor.py:136`) - no log line;
- extension route: `AskUserResponder._ask` returns `""` on
  `asker is None` (`raven/acp_client/ask_user.py:168`) - no log line.

Both are silent: the host log records nothing, only the frame journal shows a
question was asked and answered empty. For dr this degrades to "asked one
fewer time"; for raven-code's write authorization it is a hard block.

### Why origin is the wrong proxy

A `SUBAGENT` relay turn is a delegated result re-entering *the user's own
conversation* (`_inject` routes by `conversation = origin["session_key"]`).
Whether a human can answer the questions it spawns has nothing to do with the
turn's origin and everything to do with whether a surface is currently
watching that conversation. Two facts decide the fix:

- The last-sender owner registry (`raven/rpc/connection.py:46`, `_owners`) is
  the wrong signal: it is claimed only by `turn.send`, and a TUI restarted
  after the last message owns nothing. The 08:20 state (restarted, no message
  sent) would still be declined under an owner-based gate.
- The subscription registry is the right signal: the TUI opens a
  `turn.subscribe` per conversation it is viewing
  (`raven/rpc/methods/turn.py` wraps `SubscriptionEmitter.register`), and the
  emitter holds `_by_session: session_key -> [Subscription]`
  (`raven/rpc/subscriptions.py:61`), dropping the bucket when the last
  subscription closes (`_mark_closed`, `subscriptions.py:200-204`). Watching
  does not require speaking.

### Sentinel turns already act correctly (a verified non-change, re-verified on this branch)

The proactive engine submits exactly two turn shapes:

- a user-accepted menu pick (`action_executor._execute_reply`,
  `raven/proactive_engine/sentinel/executor/action_executor.py:151`) already
  runs as `origin=Origin.USER` - it binds the asker today. The
  `sentinel=SentinelExtras(action_origin=True)` marker exists so Sentinel's
  own hooks do not double-count the engagement, not for the asker gate.
- the superseded-menu notice
  (`task_discoverer.py:311`) is `origin=SENTINEL` with fixed notice text; it
  spawns no sub-agents and asks nothing.

No sentinel change is needed.

## Design

### Scope: the TUI RPC spine only

The fix changes the gate in `RpcTurnRunner.run` (`raven/rpc/spine.py`). The
gateway runner's gate stays USER-only, as an explicit non-goal:

- the incident, the watching signal, and "TUI/前端在线" all point at the TUI
  transport;
- the gateway has no watched-signal to gate on: its `gw_sources` registry
  (the clarify channel route's liveness check,
  `raven/cli/gateway_commands.py:119-148`) is populated by the runner itself
  at turn start, so gating on it would make the SUBAGENT extension
  unconditional there - abandoned-channel-session questions would regress
  from today's instant decline to a 600s stall;
- channel transports are disabled on the dev box; if a channel deployment
  ever needs the same fix, it needs a different signal and its own design.

### The gate condition

(Revised after review: a subscription outlives its socket on an abrupt
disconnect, so `has_subscribers` filters by a connection liveness registry
maintained at bind/unbind -- a watcher counts only while its connection is
bound. The gate then never opens on a stale entry, and an unattended relay
keeps today's instant decline.)


In `RpcTurnRunner.run` (`raven/rpc/spine.py:185`), widen `interactive`:

```python
interactive = isinstance(ask_tool, SupportsDirectAsk) and (
    req.origin is Origin.USER
    or (req.origin is Origin.SUBAGENT and self._emitter.has_subscribers(cid))
)
```

The same `interactive` flag already feeds both the `AskViaTool` binding and
the `Autofill` binding, so autofill widens in lockstep - correct, since it
resolves questions over the same turn-scoped channel.

`has_subscribers(session_key)` is a new read-only method on
`SubscriptionEmitter`. A subscription records the connection that registered
it (`conn_state`, captured in `register` under the dispatching socket's
context), and the predicate counts a watcher only while
`raven/rpc/connection.py`'s bind/unbind liveness registry says that
connection is still bound; a subscription registered outside any socket
counts as live. The runner already holds this emitter (`__init__` argument),
and it is the same instance `turn.subscribe` registers into
(`raven/rpc/bootstrap.py:167` creates one emitter, passed to
`build_rpc_spine` and `register_turn_methods`). No new plumbing.

### Delivery paths after the gate opens

The broker's existing `conversation_scoped` routing handles the rest:

- owner exists (someone sent a turn since the question's surface connected) -
  the sheet goes to that connection;
- no owner (the 08:20 state) - scoped send has no sink, falls back to
  broadcast (`raven/rpc/connection.py` `conversation_scoped`), and the
  watcher's connected surface shows the sheet;
- a live subscription at binding time implies a connected client, so the
  question reaches whoever is attached when it lands; if every surface has
  gone by then, the broker's 600s fail-safe
  (`raven/rpc/question_broker.py:30`) bounds the wait before the default --
  bounded, not absent.

### What stays declined

- CRON and HEARTBEAT turns: origin gate unchanged, and their conversations
  (`cron:<job_id>`) are never subscribed, so the subscription check would
  reject them even without the origin test. Fail-closed preserved by
  construction.
- SENTINEL notice turns: unchanged (fixed text, no questions).
- A SUBAGENT relay whose conversation nobody is watching: no subscription,
  gate closed, today's millisecond decline. The stall the capability exists
  to avoid (`raven/acp_client/ask_user.py` module docstring) is not
  reintroduced for unattended runs.
- The watcher leaving mid-run: the asker is captured at construction, so a
  run whose gate opened keeps its channel; a later question then routes via
  owner/broadcast and degrades to the 600s default, never a hang.

## Observability fix

The two silent decline branches get a `logger.warning` each:

- `raven/acp_client/elicitor.py:136` `asker is None or not conversation_id`
  branch;
- `raven/acp_client/ask_user.py:168` `asker is None` branch.

The incident left zero host-log trace; with these, the next occurrence names
the agent and conversation that were declined because no asker was bound.

## Files touched

1. `raven/rpc/subscriptions.py` - add `has_subscribers(session_key)`;
   `Subscription` records its registering connection.
2. `raven/rpc/connection.py` - bind/unbind liveness registry,
   `current_state()`, `is_bound()`.
3. `raven/rpc/spine.py` - gate condition in `RpcTurnRunner.run`.
4. `raven/gateway/spine.py` - comment only: the gateway keeps its USER-only
   gate.
5. `raven/acp_client/elicitor.py` - warning log on the decline branch.
6. `raven/acp_client/ask_user.py` - warning log on the decline branch.
7. Tests: `tests/test_rpc_spine.py`, `tests/test_subscription_emitter.py`,
   `tests/test_acp_ask_user.py`, `tests/test_acp_elicitation.py`.

No changes to `manager.py` (`_inject`), the proactive engine, the contracts,
or the TUI.

## Testing

- `test_subscription_emitter.py`: `has_subscribers` false when empty, true
  after `register`, false after the last subscription closes, false after
  the registering connection unbinds, true while any watcher's connection
  lives.
- `test_rpc_spine.py`: the gate matrix - USER binds (existing behavior);
  SUBAGENT with a live subscription binds; SUBAGENT without one declines;
  CRON declines. The file already carries an asker-recording harness
  ("Records what the turn bound as its asker", `test_rpc_spine.py:249`), so
  the new cases extend it.
- `test_acp_ask_user.py` / `test_acp_elicitation.py`: the no-asker branches
  now emit the warning (caplog assertion).

## Risks and accepted corners

- Behavior change: a SUBAGENT relay in a watched conversation now waits up to
  600s per question on the watcher instead of defaulting instantly. That is
  the point of the fix; the broker's timeout is the bound.
- Broadcast untidiness: with no owner, a question falls back to broadcasting
  to all attached surfaces of the transport - the existing
  `conversation_scoped` semantic, now reachable for SUBAGENT questions. In
  single-user TUI deployments a non-issue.
- A surface that subscribes but cannot render `clarify.request` (the webui
  page does not support ask_user) would open the gate and leave the question
  unanswered until the 600s default. Accepted for now; a follow-up could
  record each subscription's declared surface and filter the gate to
  surfaces that render questions.
- Rollback: restore the origin-only condition; everything else is additive.

## Decision record

- Base is `refactor/raven_v0_2_0`: the ACP client moved to
  `raven/acp_client/`, `Asker`/`SupportsDirectAsk` live in
  `raven/contracts/asking.py`, `start_ask_turn` carries an `autofill` slot,
  and the same gate now stands in two spines (rpc + gateway).
- Watched signal = live `turn.subscribe`, not the last-sender owner
  registry: the incident's TUI restart left the owner registry empty while
  the user was watching, so the owner signal cannot cover the incident.
- Sentinel `action_origin` turns: no change, verified already USER on this
  branch too.
- Gateway leg: explicit non-goal; it lacks a watched-signal, and extending
  it would regress abandoned-channel sessions to 600s stalls.
