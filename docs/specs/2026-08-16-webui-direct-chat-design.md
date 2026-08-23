# Direct chat in the web UI - design

Date: 2026-08-16
Status: proposed
Base: `feat/tui_subagent_direct_chat` at `0713ef88`, which carries the runtime
and the TUI client this extends.

## Problem

Direct chat with a sub-agent instance exists in the TUI only. The web UI can
already *see* instances -- `SubagentInstanceMonitor` renders a live list from
`subagent_instance_updated` custom events -- but cannot address one. The
runtime side is not TUI-specific (`turn.send` takes a `target`, events carry
one, `subagents.instance.history` reads the records), so what is missing is
wiring on the web channel plus a client that demultiplexes.

## Decisions

- **W1** Same interaction model as the TUI: selecting an instance switches the
  **main chat area** to that instance's transcript, with a way back to Raven.
  Chosen over a chat pane inside the instance panel: one conversation surface,
  one composer, and every rule the TUI already establishes transfers.
- **W2** A direct turn's events reach the browser **out of band**, as custom
  events, never as the main reply's content blocks. Forced by the transport:
  the service's `reply_stream` is one AgentScope reply per turn and ends on
  `message.complete`, so feeding another lane's events into it would render a
  sub-agent's answer as the main agent's and end the main reply early. Custom
  events already survive `_drop_stale_turn_events` and reach the session's SSE.
- **W3** The service **demultiplexes on `target`**. One reader drains the
  session's queue; events carrying a `target` go to the direct-chat publisher,
  untagged ones to the main reply. Today there is no `target` check anywhere in
  the web client, which is what makes W2 a correction rather than an addition.
- **W4** Direct-chat messages never enter the AgentScope session message list.
  That list *is* the main agent's transcript, and keeping these exchanges out of
  it is the whole point of the feature. A reload restores the view from
  `subagents.instance.history`, as the TUI does.
- **W5** No cancel, per the concurrent-direct-chats design D3: `turn.cancel`
  means the main agent's turn on both surfaces.
- **W6** Concurrency is inherited, not rebuilt. The web spine gets the same
  `direct_targets` map and direct pool the TUI spine has; instances answer in
  parallel and the main agent stays serial on its own lane.

- **W7** The vendored AgentScope tree is editable, and this deliberately does
  not edit it. The one thing that freedom could buy is making a direct chat a
  *real* AgentScope reply -- persisted, reloadable, rendered by the existing
  block components. Its blocker is a single line, `_chat.py`'s
  `acquire_lock(session_lock(session_id))`, a **blocking** per-session mutex, so
  a direct reply would queue behind the main agent's instead of running beside
  it. Re-keying that lock by target is the same move raven's spine made for
  lanes, but the lock guards more than the reply loop -- `upsert_message`, the
  `reply_msg` accumulation, the awaiting-tool-call check and the inbox drain --
  so two replies under different keys would mutate one session's message
  storage and tool-call state concurrently. That is a change to a vendored
  framework's core concurrency model, reaching every web feature (DAG, HITL
  confirm, deliverables), with no existing coverage of concurrent replies to
  catch a regression. Not worth it for rendering reuse, which W2 gets anyway by
  mapping the custom events into the same `Msg` shape the bubbles already
  render. Revisit only if direct chats must survive a reload without refetching,
  and then as its own change with its own concurrency tests.

  Worth recording because the obvious objection to W2 does *not* apply:
  `RavenGatewayAgent` holds no memory and relays only the new text, so the
  AgentScope message list is the web UI's own display store and raven never
  reads it. Putting direct chats there would not pollute the main agent's
  context -- the reason to keep them out of it is the lock, not contamination.

## Runtime gaps this has to close first

Both are wiring omissions on the web channel, not new mechanism:

1. `web_dispatcher` (`raven/cli/gateway_commands.py`) registers system, turn and
   config methods only. `subagents.instances` and `subagents.instance.history`
   are absent, so the browser cannot list instances or read a transcript over
   its own connection.
2. The web branch never creates a `direct_targets` map, and `build_web` does not
   accept one. `RpcOutlet` therefore holds an empty dict and tags nothing, while
   `register_turn_methods` writes into a second empty dict of its own. The two
   must be the same object -- which is exactly what `raven/rpc/bootstrap.py` and
   `raven/cli/tui_commands.py` already do -- or no event ever carries the
   `target` the client demultiplexes on.

## Client model

Mirrors `ui-tui/src/app/directChatStore.ts`, in React idiom:

- one active target (`null` = the main agent) plus a transcript per instance,
  keyed the same length-prefixed way so two handles cannot collide;
- the composer sends `turn.send` with `target` while a target is active;
- `busy` is a property of the view on screen, not of the session, so the
  composer stays live in one view while another instance answers;
- a second prompt to the instance that is mid-reply is refused, with a message
  in that view. Every other combination is allowed.

## Follow-ups (not this change)

- Attachments in a direct chat, which the TUI also defers.
- Reclaiming direct-chat record directories, deferred with the same reasoning as
  in the TUI design.
