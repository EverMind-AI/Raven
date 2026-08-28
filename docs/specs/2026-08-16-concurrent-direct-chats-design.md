# Concurrent direct chats - design

Date: 2026-08-16
Status: implemented
Base: `feat/tui_subagent_direct_chat` at `d7046d51`, which carries the direct-chat
feature this builds on.

## Problem

A direct chat is a turn on the session, and the runtime holds one turn slot per
session, so talking to one sub-agent instance blocks talking to any other. In
use this reads as broken rather than as a rule: the composer refuses, and before
`d7046d51` it did worse - the busy-input policy acted on the other
conversation's turn and the default mode tore that reply down.

The instances themselves are independent processes and sessions. Nothing about
them requires serialising their conversations.

## Decisions

- **D1** Everything runs concurrently: the main agent's turn and any number of
  direct turns. The main agent stays one turn at a time, which is its own lane's
  existing property, not a new rule.
- **D2** Direct turns draw on a **separate concurrency pool** from user turns.
  Several sub-agents answering must not make the user queue behind them to say
  one sentence to Raven.
- **D3** A direct turn is **not cancellable**. `turn.cancel` and Ctrl+C keep
  meaning "the main agent's turn", as they do today. Consistent with dropping
  the instance-scoped cancel earlier: a sub-agent that is answering is left to
  finish, and its record is the evidence either way.
- **D4** Two turns to the *same* instance still serialise. That is already true
  and comes from `hold_handle`, which binds one CLI/ACP session to one turn at a
  time; nothing here changes it. What the client adds is refusing the second
  send rather than queueing it behind an unbounded wait.
- **D5** No wire-protocol change. The conversation a client subscribes to stays
  the session; every event already carries the `target` it belongs to, and the
  view demultiplexes on that today.

## Where the serialisation actually lives

Not in the scheduler. `Scheduler._conversation_id` keys a lane on
`req.conversation`, and the docstring already contemplates a channel keying by
"a sub-conversation within a chat". Four session-keyed places are what serialise:

1. `turn.py: _active_turns[session_key]` - the -32003 guard.
2. `spine.py: turn_ids / usages / direct_targets` - one slot per conversation;
   two concurrent turns would overwrite each other's turn id and addressee.
3. The sink's `hub.close_stream(conversation_id)` at turn end, which would cut a
   still-streaming sibling.
4. `OriginPools(user=1)` - the USER semaphore, which serialises even separate
   lanes.

## Design

**Lane key.** A direct turn runs on `f"{session_key}#{agent}/{handle}"`; the
main agent keeps the bare session key. Derived in `turn.send`, so the scheduler
is untouched. `session_of(lane)` splits on the **first** `#`: a session key is
`channel:chat_id` and never contains one, while a handle is free-form text the
model chose and may contain anything.

With that key, (1), (2) and (3) become per-instance for free - they are all
keyed by conversation already.

**Emitting back.** The lane stamps its events with the lane key, so `RpcOutlet`
emits to `session_of(conversation_id)`. The client's single subscription
therefore keeps receiving every conversation's events, tagged as it already
expects. This is what makes D5 hold.

**Session identity.** `AgentLoop.run_turn` currently derives everything from
`cid` - the direct-chat record directory, the instance registry key, the
handoff. Those must keep using the session, so the direct branch resolves
`session_of(cid)` and uses it for all three. Getting this wrong would scatter an
instance's records across per-instance directories and land the handoff on a
conversation the user never reads.

**Pool.** `OriginPools` gains a direct pool, chosen per request rather than per
origin: a direct turn is still `Origin.USER` and inventing an origin for it
would need a home in every origin switch in the codebase.

## Client

`uiState.busy` is one boolean read in 21 places, and `turnController` holds one
buffer. Both become per-view:

- busy is a property of the view you are looking at, so the composer is live in
  view B while A is answering;
- the pause message survives, but only for the same-instance case (D4);
- the chip strip already marks running instances independently of which one you
  are on, and needs no change.

## Follow-ups (not this change)

- Concurrent turns to the *same* instance, which would need a queue per instance
  rather than a refusal, and a story for what a second prompt means to a CLI
  session mid-answer.
- Cancelling a direct turn (D3), if the need appears.
