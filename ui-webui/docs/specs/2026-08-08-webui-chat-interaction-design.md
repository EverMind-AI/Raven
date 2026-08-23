# Web UI chat interaction — gap audit and design

**Date:** 2026-08-08
**Status:** Draft (design)
**Companion:** `2026-08-08-webui-tool-surface-gaps-design.md` (the tool surface;
this document is the conversation lifecycle around it). Gap ids there are `G*`,
here they are `I*`; `I3` is the same defect as `G12` seen from this side.

## 1. Motivation

The tool audit covered what happens *inside* a turn. This one covers the turn
itself: sending, streaming, stopping, failing, reloading. The trigger is the stop
button, which does not stop anything - but pulling that thread turns up a set of
defects that share one root cause and one theme: **the web UI reports states that
are not true**. A stopped reply is reported as still running, forever. A reply cut
short by an idle clock is reported as complete. A message the agent never received
is shown in the history as sent. A gateway that is healthy is reported as
unreachable.

## 2. How this was verified

Static reading of `origin/main` plus a live run of the full stack on 2026-08-08.
Findings marked **(observed)** were reproduced in the browser against a running
gateway; the rest are structural.

## 3. Scope

**In scope** - the lifecycle of one web chat turn: `POST /chat/` through
`turn.send`, the SSE event stream, the interrupt path, terminal events, what is
persisted, and what a page reload reconstructs.

**Out of scope** - the AgentScope-native agent path, team/HITL flows that do not
apply to the gateway agent, and everything already covered as `G*` in the
companion spec.

## 4. Gap inventory

Severity: **A** = the UI states something false, or user input is lost;
**B** = correct but fragile or misdiagnosed; **C** = hygiene.

| id | Gap | Sev |
|---|---|---|
| I1 | `RavenGatewayAgent.reply_stream` has no cancellation cleanup, so an interrupt produces no `ReplyEndEvent` | A |
| I2 | An interrupted reply becomes a permanent "Running..." zombie, surviving reloads | A |
| I3 | Stop never reaches the gateway, so the turn keeps running (`G12`) | A |
| I4 | Even with `turn.cancel` wired, a running `exec` subprocess survives cancellation | A |
| I5 | `turn.cancel` awaits full unwind, but the RPC client gives up after 30 s | B |
| I6 | The 10 s interrupt fallback clears the phase but not the current-reply ref | B |
| I7 | A message rejected with 409 is silently lost | A |
| I8 | A message rejected by the gateway is persisted web-side only, so the two histories diverge | A |
| I9 | The 900 s idle clock reports a truncated turn as completed | A |
| I10 | A mid-turn gateway disconnect is also reported as completed, in hardcoded Chinese | B |
| I11 | The SSE stream never reconnects | B |
| I12 | `GatewayClient` leaks a queue and a gateway subscription per reconnect | C |

### I1 - no cancellation cleanup in the gateway agent

The framework's contract is explicit. `ChatService._run_impl` catches a
`CancelledError` that lands on the *publish* side and deliberately re-arms it
(`_service/_chat.py:584-592`):

> Re-arm it so it's redelivered into the agent at the next `__anext__` (which runs
> its interruption cleanup) instead of abandoning the generator and dropping that
> cleanup.

So the agent is expected to catch cancellation and yield its own terminal
`ReplyEndEvent`. `RavenGatewayAgent.reply_stream` does not: the cancel lands in
`await asyncio.wait_for(queue.get(), ...)`
(`ui-webui/service/raven_gateway_agent.py:330`) and propagates straight out of the
generator, so the `_close_block()` and `ReplyEndEvent` at lines 427-430 never run.

Everything in I2 follows from this.

### I2 - interrupted replies are permanent zombies (observed)

`MessageBubble` derives its running state from the message alone:

```ts
const isRunning = !message.finished_at;                 // MessageBubble.tsx:726
```

`finished_at` is set when a `ReplyEndEvent` is applied. With I1 there is no such
event, so:

- the bubble keeps a spinner and a live-ticking elapsed timer;
- `_persist()` in `_run_impl`'s `finally` writes that reply to storage as-is, with
  unterminated content blocks and no `finished_at`;
- every future page load reconstructs it from storage and starts ticking again,
  with a `setInterval` per zombie (`MessageBubble.tsx:733-737`).

**(observed)** After a reload, two replies stopped earlier in the session were
still rendered as running:

```
aria-label="Running…"  ->  "50m42s"
aria-label="Running…"  ->  "1m57s"
```

A reload does not clear them, and nothing ever will - the state is on disk.

### I3 - stop never reaches the gateway (observed)

`GatewayClient` exposes `subscribe` and `send_turn` only; `turn.cancel` is
registered on the web dispatcher and never called. The interrupt cancels the local
chat-run task, so the service stops *reading*, while the gateway turn runs to
completion. Documented as `G12`; the consequence that belongs to this audit is
what the *next* message does:

**(observed)** Stopping a turn and immediately sending again produced this reply:

```
连接 gateway 失败: session 'web:a3941d7cd34044ab97b2ef6693a61fa0' already has an active turn
```

Two false statements in one line. The gateway connection is fine, and the error is
not a connection error - it is `turn.send` correctly rejecting a second turn with
`-32003` (`raven/rpc/methods/turn.py:139-142`), surfaced through
`reply_stream`'s connect/submit `except` branch, which labels every failure a
connection failure (`raven_gateway_agent.py:311-317`).

### I4 - a cancelled turn leaves the subprocess running

This is why wiring `turn.cancel` is necessary but not sufficient.
`DirectExecutor.execute` kills the child **only** on `asyncio.TimeoutError`
(`raven/sandbox/direct_executor.py:102-110`). There is no `except
asyncio.CancelledError` and no `finally`, so cancelling the turn task abandons the
`await process.communicate()` and orphans the process.

Worse, even the timeout path cannot clean up a tree: the child is started with
`create_subprocess_shell` and no `start_new_session`, and `process.kill()` signals
that one pid. A command like `bash -c 'sleep 90; ...'` is three processes -

**(observed)** during the stop probe:

```
/bin/sh -c bash -c 'sleep 100; echo ... > /tmp/stoptest.txt'
bash -c sleep 100; echo ... > /tmp/stoptest.txt
sleep 100
```

- so killing the `sh` leaves `bash` and `sleep` alive. The sentinel file appeared
on schedule after the stop, which is the same evidence read from the other end.

### I5 - cancel can time out even when it works

`turn_cancel` deliberately awaits the handle so the active-turn slot is provably
cleared before returning (`turn.py:255-257`), while `GatewayClient.call` gives up
after 30 s (`raven_gateway_agent.py:179`). A turn whose unwind is slow - which,
per I4, is exactly the interesting case - makes the cancel RPC raise client-side
although the gateway completed it. A stop implementation that treats that
exception as failure will report a false failure.

### I6 - the interrupt fallback half-resets the client

`interrupt()` optimistically moves the phase to `interrupting` and arms a 10 s
timer that reverts to `idle` if no terminal event arrives
(`useMessages.ts:449-467`, `INTERRUPT_TIMEOUT_MS = 10_000`). With I1 that timer is
always what ends the interrupt. It resets `phase` but not `currentReplyRef`, so:

- the stale reply stays the append target: any late event lands on the wrong
  message;
- the composer is enabled again while the gateway turn is still running, which is
  precisely how a user walks into I3.

`useMessages` also keeps an `error` state that no component reads, so the hook's
own error reporting is dead code; what the user actually sees comes from the API
client's automatic `toast.error(detail)` (`api/client.ts:98-99`).

### I7 - a 409'd message is silently lost (observed)

`POST /chat/` raises 409 *before* `chat_service.run`, so the user message is never
persisted (`_router/_chat.py:142-156`). The frontend appended it optimistically
and does not remove it (`useMessages.ts:361-378`), so the bubble stays on screen
and the user believes it was sent.

**(observed)** An answer typed into a session blocked on `ask_user` showed as a
bubble, returned 409, and was **gone after a reload** - no record anywhere. The
only feedback is a toast carrying an internal string
(`Session '...' already has an active chat run in this process.`), which is not
i18n'd and does not say the message was dropped.

### I8 - a gateway-rejected message diverges the two histories (observed)

The opposite ordering, and worse. When the local run slot is free but the gateway
turn is not (the state I3 leaves behind), `/chat/` accepts, `upsert_message`
persists the user message, and then `turn.send` fails. The web UI's history now
contains a message the Raven session has never seen. The two sides keep separate
histories, so this divergence is permanent, and the user is told it was a
connection failure.

### I9 - the idle clock reports a truncated turn as complete

`timeout_seconds = 900`. When no event arrives for 900 s and no blocking tool is
in flight, the loop `break`s and the generator yields
`ReplyEndEvent(finished_reason=COMPLETED)` (`raven_gateway_agent.py:328-332`,
`430`). Upstream the turn continues. So:

- the user sees a truncated answer marked complete;
- the real answer arrives on a queue nobody reads, and the next turn discards it
  through `_drop_stale_turn_events`;
- until the upstream turn ends, the next message hits I3/I8.

Blocking tools suspend this clock, which is why `ask_user`'s 600 s waits survive
it. Non-blocking work does not: `exec` alone can be granted up to
`_MAX_TIMEOUT = 600` s, and two long calls in one turn with no interleaved output
are enough.

### I10 - a disconnect is also "completed"

The `__disconnected__` branch appends `[gateway 连接中断,本轮结束]` and ends the
reply with `COMPLETED` (`raven_gateway_agent.py:404-413`). The reason is wrong, and
both this string and I3's are hardcoded Chinese written into reply content, so they
bypass i18n and are persisted into history verbatim.

### I11 - the SSE never reconnects

`sessionApi.streamEvents` is a single `fetch` whose body is read to completion
(`api/session.ts:67-95`); `useMessages` iterates it once and, on normal
completion, neither retries nor sets an error (`useMessages.ts:330-343`). The
server side is long-lived and heartbeats every 30 s
(`_router/_session.py:850-861`), so this does not bite in a healthy deployment -
but a service restart, a proxy, or a network blip ends the stream and the session
goes deaf until the component remounts. There is no indication in the UI.

Replay itself is sound: the stream first replays the current run's buffered events
and the run's log is dropped at turn end (`log_trim(events_key)` with no
`before_id`), so a reconnect mid-turn reconstructs the partial reply and a
reconnect after a turn does not double-render it.

### I12 - reconnect leaks queues and subscriptions

`ensure_connected` clears `_subs` so subscriptions are re-issued after a
reconnect, but does not clear `_queues` (`raven_gateway_agent.py:130-132`). Each
reconnect therefore leaves the previous queue behind, and the read loop's
`finally` fans `__disconnected__` into those dead queues too. The gateway side is
never told either - nothing calls `turn.unsubscribe` - so its emitter accumulates
one registration per session per reconnect.

## 5. Decisions

1. **The gateway agent owns its interruption cleanup** (2026-08-08) - wrap
   `reply_stream`'s loop so cancellation closes any open block and yields
   `ReplyEndEvent(finished_reason=INTERRUPTED)` before propagating. This is the
   single fix that retires I1, I2 and I6's stale-ref hazard, and it is what the
   framework already assumes.

   Note the generator must still let the cancellation propagate after yielding -
   swallowing it would leave `_run_impl` believing the run completed normally.
   Yielding from an `except CancelledError` block is legal in an async generator
   as long as the exception is re-raised afterwards; the cleanup must not await
   anything that can block.

2. **`finished_at` is the frontend's single source of truth, and stays that way**
   (2026-08-08) - no client-side heuristic ("older than N minutes, assume dead").
   The terminal event is the fix; a UI guess would paper over a lost event and
   make the next bug invisible.

   The zombies already on disk are a separate, one-off concern: they are stored
   messages with no `finished_at`. Backfilling them is out of scope; note in the
   plan that existing sessions keep their zombies unless someone writes a
   migration.

3. **Stop is three fixes, not one** (2026-08-08) - cancel upstream (I3), have the
   executor kill its process group (I4), and tolerate the cancel RPC's 30 s
   deadline (I5). Shipping only the first would produce a stop that reports
   success, ends the UI turn, and leaves the user's command running - the current
   behaviour with a better-looking UI.

4. **The executor kills the process group, not the pid** (2026-08-08) - start the
   child with `start_new_session=True` and signal the group on both the timeout
   and the cancellation path, escalating `SIGTERM` -> `SIGKILL`. This is a Raven
   core change and it fixes the TUI and IM surfaces at the same time.

   Rejected: killing only the direct child, as today. A shell wrapper is the
   normal shape of an `exec` call, so pid-only killing leaks by default rather
   than by exception.

5. **A failed `turn.send` must not be reported as a connection failure**
   (2026-08-08) - split `reply_stream`'s connect/submit `except` into the cases it
   actually has: no socket, `-32003` (a turn is already running), `-32008` (no
   model), and everything else. `-32003` in particular is a state the user can act
   on ("the previous turn is still running"), and after decision 3 it should stop
   occurring at all.

6. **A rejected message is removed from the transcript, not left in it**
   (2026-08-08) - on a failed trigger, `send()` drops the optimistic bubble and
   surfaces a specific message. Leaving it is what makes I7 a silent loss and I8 a
   permanent divergence.

   Rejected: keeping the bubble with a retry affordance. It reads as "sent, will
   retry" and would need a queue with its own ordering semantics against the
   agent's history. Out of proportion to the problem.

7. **The 409 window closes once the phase is honest** (2026-08-08) - with
   decision 1 the client's phase tracks the real turn, so the composer is blocked
   while a turn runs and 409 becomes unreachable through the UI. Keep the server
   guard (it is a correct double-submit defence) but treat reaching it as a bug
   signal, and make its message say what happened rather than exposing
   `ChatRunRegistry`'s internal wording.

8. **The idle clock must not claim completion** (2026-08-08) - when the 900 s
   timeout fires, end the reply with `INTERRUPTED` and a translated notice, and
   cancel the upstream turn (the same call decision 3 adds) so the client and the
   gateway agree on the turn being over. Same for `__disconnected__`.

   Rejected: raising the timeout. Any finite clock has this failure; the bug is
   the label and the missing upstream cancel, not the number.

9. **User-visible strings go through i18n** (2026-08-08) - the service emits a
   machine-readable reason and the frontend renders the text, instead of the
   service writing Chinese prose into reply content that is then persisted.

10. **The SSE reconnects with backoff** (2026-08-08) - on stream end while the
    component is still mounted, retry with capped backoff and show a
    reconnecting indicator. Replay makes this safe: the log rebuilds the current
    run and is empty once the run ended.

11. **The WS client cleans up after a reconnect** (2026-08-08) - drop stale queues
    when `_subs` is cleared, and best-effort `turn.unsubscribe` before discarding
    a subscription. Low value alone; bundle it with whichever phase touches
    `GatewayClient`.

## 6. Interaction with the tool-surface spec

| This spec | Companion |
|---|---|
| I3 decision 3 (cancel upstream) | `G12` decision 12 - same call site; implement once |
| I1 decision 1 (terminal event on interrupt) | prerequisite for `G12` being observable in the UI |
| I5 (cancel RPC deadline) | refines the companion's "wrap the RPC in try/except" |
| decision 8 (cancel on idle timeout) | reuses the same cancel path |
| pending `ask_user` question on cancel | companion decision 5 - release it in the same cancel handler |

The stop work should therefore land as one change spanning both specs, not as two.

## 7. Risks

- **Yielding during cleanup** (decision 1). An async generator that yields while
  handling `CancelledError` is easy to get subtly wrong: if the consumer is
  already gone, the yield raises and the cleanup is lost anyway. The test must
  cover cancellation arriving both at `__anext__` and at the publish side (the two
  cases `_run_impl` distinguishes).
- **Killing process groups** (decision 4) is a core change on a path every surface
  uses. A too-eager group kill could take out something the tool did not start -
  `start_new_session=True` is what makes the group exactly the tool's own
  children, so it must land in the same change, not after it.
- **Zombie backfill** (decision 2). Deciding not to migrate means existing
  sessions keep spinning bubbles forever. That is a visible-forever artifact and
  the user should confirm they accept it, or ask for the migration.

## 8. Test strategy

- **Core (pytest):** `DirectExecutor` kills the whole group on cancellation and on
  timeout (assert on a shell wrapper that spawns a grandchild, not on a bare
  command); `turn_cancel` still clears the active slot when the unwind is slow.
- **Service:** `reply_stream` yields `ReplyEndEvent(INTERRUPTED)` when the task is
  cancelled at `__anext__` and when cancelled at the publish side; a `-32003`
  submit surfaces as its own reason rather than "connection failed"; the idle
  timeout ends the reply as `INTERRUPTED` and calls `turn.cancel`.
- **Frontend:** no unit runner, so `pnpm -C frontend lint` (0 errors) plus
  `pnpm -C frontend build`, and a manual matrix:
  1. stop a long `exec`: the bubble settles into a finished state, the process is
     gone (`ps`), the sentinel file never appears, and the next message is
     accepted normally;
  2. reload after a stop: no ticking timer;
  3. send while a turn runs: the composer refuses rather than 409;
  4. kill the service mid-stream: the UI shows reconnecting and recovers.
- **Regression:** the executor change affects the TUI and IM surfaces; run a TUI
  `exec` and confirm normal completion and normal timeout behaviour.
