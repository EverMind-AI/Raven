# TUI sub-agent direct chat - design

Date: 2026-08-13
Status: implemented
Base: rebased onto `origin/main` on 2026-08-15. Line anchors below were taken
on the earlier base `84884632` and may have shifted; upstream has since renamed
`raven/tui_rpc/` to `raven/rpc/` and moved the schema to `rpc-schema/`.

## Absorbed by upstream

An earlier revision of this spec carried a "Ported prerequisite" section: two
helpers this design leans on (`hold_handle`, and `CliAgentBackend._run_stateful`
with its `resumable` gate) existed only on an unpushed local branch, so the plan
ported them verbatim as its first task.

Upstream has since landed both, and further developed them - `_run_stateful` now
takes the lock only for a named handle, because a DAG node holds its dispatch
slot while it waits and locking an author-chosen id would stall unrelated runs.
The rebase therefore dropped that task's commit entirely, and upstream's tests
for the behaviour are better than the ported ones were. Porting verbatim rather
than reimplementing is what made that a `git rebase --skip` instead of a merge.

## Goal

Let the user talk to one of this session's sub-agent instances directly from
the TUI, then hand the main Raven agent a pointer to what happened while it
was not looking.

Three parts:

1. A strip above the composer listing the sub-agent instances this session has
   used, and a way to switch into a direct conversation with one of them.
2. A direct-chat mode that takes over the chat view: same composer, the
   instance's own transcript, Esc to return.
3. A handoff block the runtime prepends to the user's next turn to the main
   agent, naming each direct-chat segment, its UTC time span, and the absolute
   paths of its per-turn input and output files.

## Why this is needed

The main agent already delegates to sub-agents, and the user can already watch
that happen (`/agents` renders the spawn tree). What the user cannot do is
*talk to* one. Today the only way to correct a sub-agent mid-flight is to ask
the main agent to re-delegate, which restarts the sub-agent's reasoning and
burns the main agent's context re-explaining it.

Direct chat also keeps that correction *out* of the main context. A long
back-and-forth with a coding sub-agent about which file to touch is exactly
the sort of detail the main agent should not have to carry, which is why the
handoff is a pointer and not a transcript.

## Scope

In:

- Direct chat with three kinds of instance: `cli`, `openai`, and the built-in
  `raven-loop` sub-agent.
- Session resumption for `openai` and `raven-loop` instances (they have none
  today).
- Registry rows for `raven-loop` instances (they have none today).
- Four new TUI-RPC methods, one changed method, one new `TurnRequest` field.
- The handoff mechanism, held in the runtime.
- The TUI surface: chip strip, mode takeover, keybindings, greying.

Out:

- Streamed direct-chat replies, for any kind. See "Decisions" D6 - since
  superseded, see "Streaming".
- Any report of which files a direct chat modified. See D7.
- A web UI surface. The mechanism lives in the runtime so a later web surface
  can adopt it, but no web UI work is in this change.
- Direct chat with a DAG node.

## Decisions

- **D1** An instance is the existing `(session_key, agent, handle)` triple
  (`raven/agent/subagent/instances.py`). No new identifier.
- **D2** `raven-loop` instances register under the reserved agent name
  `raven`. `SubagentManager` writes registry rows for them, which it does not
  do today (`manager.py:238` gates on `if agent:`).
- **D3** `stateful` becomes a per-kind capability instead of an alias for
  "has a `resume_command`". Sessions for `openai` and `raven-loop` are owned
  by Raven and replayed from a local `messages.json`; `cli` sessions stay
  owned by the CLI and resume through `resume_command` plus the registry's
  `agentId`.
- **D4** A direct-chat turn runs on the existing turn pipeline, selected by a
  new `TurnRequest.direct_target` field, and is **never written to the main
  session transcript**.
- **D5** The handoff is held by the runtime, not the client. The client only
  renders a count.
- **D6** No direct-chat reply is streamed in this change, for any kind. Each
  of the three would need separate work; see "Streaming".
  **Superseded 2026-08-16**: all four kinds can stream, `cli` conditionally on
  what its configured command asks its CLI for. See "Streaming".
- **D7** No modified-file reporting. There is no reliable source for it.
- **D8** Esc always means "return to the main agent", whether or not a direct
  turn is in flight. Cancelling a sub-agent is a separate explicit action.
- **D9** The handoff carries pointers only - instance name, UTC time span, and
  file paths. No transcript text and no generated summary. The main agent has
  read tools and can open the files it cares about; injecting the text would
  spend the context that direct chat exists to save.

## Architecture

### Instance identity and resume mechanics

| kind | `agent` value | registry row today | resume credential |
|---|---|---|---|
| `cli` | the configured name | yes | `agentId` (the CLI's own session id) |
| `openai` | the configured name | yes | local `messages.json` |
| `raven-loop` | `raven` (reserved) | **no** | local `messages.json` |

Two existing gates had to move for D3:

1. `third_party_agent_meta` (`backends/__init__.py`) derived `stateful` from
   `resume_command` alone. Upstream has since made it kind-aware for its own
   reasons (`acp` reads a probed capability snapshot), so this change merges the
   `openai` rule into that dispatch rather than replacing it.
2. `ThirdPartyOpenAISubagentConfig._reject_declared_stateful`
   (`config/schema.py`) actively raised on `stateful: true` for `openai`
   configs, on the grounds that "an HTTP agent has no resumable session". Once
   Raven replays the history itself that is no longer true, so the validator
   became `_allow_replayed_state` and permits it.

Statefulness for `openai` is per-endpoint, and that is the one place this design
departs from the rule upstream states for the other kinds ("read from the
mechanism that would have to deliver it, never from a declaration"):

- The delivering mechanism - Raven replaying the stored message list - works
  against *every* openai endpoint, so the mechanism cannot be what tells them
  apart.
- What differs is whether an endpoint is *meaningful* under replay, and no probe
  can reach that. `mirothinker` ignores a system prompt entirely, so replaying a
  transcript at it continues nothing.
- So it is declared, and the declaration **defaults to `true`**: what delivers
  the capability is Raven's own replay, which is present at every endpoint, so
  defaulting off would advertise a capability absent that is in fact there. A
  `false` is the endpoint-specific exception - a **preset states it for the
  endpoint it names** (`mirothinker` is pinned `stateful: false`), and the
  operator of a **custom endpoint** writes it by hand if they know replay is
  meaningless there. The declaration is not a wish here; it is the only
  available carrier of a fact Raven cannot discover.

This is a user-visible change: `stateful` appears in the roster text that
`spawn` and `run_subagent_dag` advertise to the model
(`format_agent_listing`), and three call sites derive it from the one helper.

**No UI surface writes this field, by decision.** Neither the `/subagents`
overlay nor the web form nor `subagents.add` / `subagents.update` carries it, so
every custom `openai` entry created through a UI is stateful; overriding that
means editing `~/.raven/config.json`. The alternative - a checkbox on both forms
plus an RPC field - would ask every user to answer a question about their
endpoint that only an operator who has already watched it ignore a replayed
transcript can answer, and would answer it wrongly by default for everyone else.

One consequence to keep in view: the web instance monitor derives its stateful
set from `kind === 'cli' && resumeCommand`
(`ui-webui/frontend/src/components/subagent/SubagentInstanceMonitor.tsx:261`),
so an `openai` instance is not shown there as stateful. That is pre-existing and
web-side; this change is TUI-only and does not touch it.

### On-disk layout

Everything lands under the session's existing metadata directory
(`raven/agent/subagent_history.py`), which is a protected subtree that no tool
write can reach:

```
<session_dir>/subagents/
|-- spawn/<call_id>/              existing: main-loop spawn records (unchanged)
|-- mas_dag/<run_id>/             existing: DAG run records (unchanged)
`-- direct/<agent>/<handle>/      new: one instance's direct chat
    |-- messages.json               resume state (openai / raven-loop only)
    `-- <call_id>/                  one direct turn, same shape as spawn/<call_id>/
        |-- prompt.md                 the handoff's "input file"
        |-- out.md                    the handoff's "output file"
        `-- meta.json                 started_at_ms / ended_at_ms / status / agent / handle
```

Three points:

- **One turn is one `<call_id>/` directory**, not an append to a single file.
  The handoff has to name per-turn input and output files, which requires a
  directory per turn; and this shape is identical to `spawn/<call_id>/`, so
  the existing `SpawnRecord` class is reused rather than reimplemented.
- **`messages.json` is separate from the turn directories.** The first is
  mutable resume state, the second is an append-only audit record. Sharing one
  file would let a single crash destroy both.
- **`cli` instances have no `messages.json`.** Their session belongs to the
  CLI; Raven stores only the `agentId` the registry already holds. Whoever
  owns the session owns its persistence.

Lifetime is split across two stores, and only one of them is reclaimed today.
`InstanceRegistry.delete_session` (`raven/agent/subagent/instances.py:183`)
removes that session's rows from the registry's own JSON file, which is what
makes the chip strip empty out and is why it survives a TUI restart (the
registry is durable across processes). But `SessionManager.delete`
(`raven/session/manager.py:483`) only unlinks the session's transcript file
and invalidates the cache; it never touches the session's metadata directory.
So `direct/<agent>/<handle>/` and its `messages.json`, like the pre-existing
`spawn/<call_id>/` and `mas_dag/<run_id>/` trees beside them, are left on disk
after the chat session that created them is deleted. There is no TTL and no
count cap either. Reclaiming them is deferred: `SessionManager.delete` would
have to recurse into the metadata directory to clean them up, and that would
also start deleting the spawn and DAG audit trails that intentionally outlive
session deletion today. That is a separate change with its own blast radius.

### Execution path

`TurnRequest.deliver_text` (`raven/spine/turn.py:60-64`) is the precedent: set
it, and the turn skips the model entirely via an early return in
`AgentLoop.run_turn` (`raven/agent/loop/main.py:3303`), while still running
through the lane and emitting through the same `emit`. Direct chat adds a
sibling field:

```
TurnRequest.direct_target: (agent, handle) | None
```

`run_turn` grows a `direct_target` branch beside the `deliver_text` branch,
dispatching to `SubagentManager.chat(...)`. This inherits three things for
free: the `_active_turns[session_key]` mutual exclusion
(`tui_rpc/methods/turn.py:102`), the `SubscriptionEmitter` routing, and the
lane's serialization of session writes.

One deliberate difference from `deliver_text`: that branch calls
`self._save_turn(session, ...)`. The direct branch must not. Writing direct
turns into the main transcript would defeat the whole point and contradict the
pointer-only handoff. A direct turn writes only its `direct/.../<call_id>/`
record.

### `SubagentManager.chat()`

A new method alongside `spawn()`, returning synchronously rather than
scheduling a background task:

```
chat(session_key, agent, handle, text, on_token=None) -> str
```

It holds `hold_handle(session_key, agent, handle)` for its whole body, so a
direct chat and a main-loop spawn on the same handle serialize against each
other through the existing lock rather than a new one. Dispatch by kind:

- **cli** - reuse `CliAgentBackend._run_stateful` with `resumable=True`. This
  path already works; almost nothing changes.
- **raven-loop** - load `messages.json` (falling back to the system prompt
  `build_subagent_prompt` produces), append the user message, run the loop,
  write back. Today `messages` is a local variable
  (`backends/raven_loop.py:186`) and has to be lifted into an injectable,
  persistable list.
- **openai** - same replay, append, request, write back. Today
  `backends/openai_api.py:60` starts from an empty list every call, so the
  change is small.

The `spawn` path must also write `messages.json` on completion for
`raven-loop` and `openai`. Without that, "direct chat continues the context of
that spawn" is false and the first entry into an instance is blank.

### Streaming

Added 2026-08-16, superseding D6. The client needed no change for it: a direct
reply already arrived as a `token.delta` carrying the instance's tag, and the
view cannot tell one frame from five hundred.

`SubagentBackend` gained an optional `on_delta` callback and a `streams` class
attribute. `streams` is read from the transport, never from config - the same
rule `AgentMeta.live_progress` follows - and `SubagentManager.chat` offers the
callback only to a backend that declares it, so a transport that cannot stream
is never asked to pretend. The loop's direct branch emits `StreamDelta` and
withholds its closing `Text` only if something streamed; a backend that ignores
the callback therefore takes exactly the path it took before. The fallback is
the absence of deltas, not a branch.

Per kind:

- `RavenLoopBackend` streams via `provider.chat_stream`, driven by
  `stream_llm_call` - the main loop's own `_llm_call_stream`, extracted to
  `raven/agent/loop/streaming.py` so the two cannot drift. Only a caller that
  asked to watch gives up the retry ladder; a spawn keeps `chat_with_retry`.
  The provider's `generation` settings are passed explicitly, because
  `chat_stream`'s signature carries literal defaults (4096 / 0.7) and a direct
  chat must answer under the same budget as the same instance's spawns. Every
  model call's text is forwarded, preamble included, so a live view shows more
  than the record replays - whether a call is the last one is knowable only
  after it has finished.
- `AcpAgentBackend` forwards the `agent_message_chunk` updates it already
  receives mid-turn. Thoughts and tool calls are not forwarded: the wire tags an
  instance on four event types and `thinking.delta` is not one of them, so they
  would be rendered into the main agent's transcript.
- `OpenAIApiBackend` asks for `stream: true` only when a callback is wired and
  parses the SSE frames; a spawn still asks for `stream: false` explicitly,
  which is what a provider defaulting to SSE (mirothinker) needs. Unreadable
  frames are skipped rather than raised on.
- `CliAgentBackend` streams when its configured command asks its CLI for
  partial output, and only then. Measured rather than assumed: `claude` emits
  `stream_event` / `content_block_delta` / `text_delta` frames under
  `--include-partial-messages`, so `claude_stream_json` can be read a line at a
  time; `codex exec --json` emits no partial event at all - a whole reply
  arrives as the `item.text` of one `item.completed` - so no flag turns it into
  a stream and no incremental reader would help. `streams` is therefore an
  instance property read from the command, and from the resume template too: a
  create that streams and a resume that does not would stream an instance's
  first turn and nothing afterwards, while a direct chat is almost entirely
  resumes.

  The buffered `communicate()` stays the spawn path; only a caller that wired
  `on_delta` gets the line pump, which reads fixed-size chunks and reassembles
  lines itself (`StreamReader.readline` raises above 64 KiB, and one transcript
  line carries a whole tool result). The reply is still parsed from the finished
  transcript, so the record, the session-id search and the recorded attempt all
  read exactly what they read before.

  Only `text_delta` frames with a null `parent_tool_use_id` are forwarded: the
  same stream repeats the finished text on `assistant` and again on `result`,
  carries `thinking_delta` and `input_json_delta`, and carries a nested agent's
  output that the run's own `result` does not contain.

What streams is capped at the same `max_output_chars` the return value is
(`bounded_delta`), so the text on screen is never text the record lacks.

## RPC surface

TUI-RPC has no instance-related method today; web-RPC has
`raven.subagents.instances` (`web_rpc/methods_config.py:181`).

| Method | Purpose |
|---|---|
| `subagents.instances` | List this session's instance rows, feeding the chip strip. **Row shape matches the web-RPC method's.** Two surfaces disagreeing about what counts as an instance is the hardest class of bug to find later. |
| `subagents.instance.history` | Read one instance's direct-chat record (per-turn role / content / timestamps / `prompt.md` and `out.md` paths). The mode takeover has to render history, especially after a TUI restart. |
| `subagents.instance.forget` | Drop an instance from the strip; wraps the registry's existing `forget`. |
| `turn.send` (changed) | Optional `target: {agent, handle}`; absent means the main agent. `TurnSendParams` is `_Strict`, so the field is additive, but `openrpc.json` and `generated.ts` must be regenerated (`gen-rpc-types.mjs`). |

No new event types. Direct chat reuses `message.start` / `token.delta` /
`message.complete` / `error`, and **those four carry `target`**. That is not
redundant: the user can switch instances mid-flight or reconnect, and a client
that cannot tell which transcript a delta belongs to will paint sub-agent output
into the main conversation.

Three details settled during implementation:

- **Those four and no others.** The direct branch in `run_turn` emits exactly one
  `Text` and returns; it produces no reasoning, tool or episode events. Tagging
  the variants a direct turn cannot emit would be decoration.
- **Absent, not null, for a main-agent turn.** Every payload the wire already
  carried keeps its shape byte for byte, so "untagged means the main
  conversation" is a property of the frame rather than a convention a client has
  to be told. `_tag` (`rpc/methods/turn.py`) and `RpcOutlet._tagged` are the two
  places that decide it.
- **The coalescer had to learn the field.** `_merge_consecutive_token_deltas`
  rebuilds a merged frame's payload from scratch, so it dropped `target` on
  every run of two or more deltas - the exact mislabelling the tag exists to
  prevent. It now carries the tag and breaks a run where the target changes.

The map itself (`conversation -> target`) is owned by the caller that wires the
spine, and handed to both `build_rpc_spine` and `register_turn_methods`: one
dict, bound by `turn.send` and dropped by the same sink that drops `turn_ids`,
so the two cannot fall out of step. The `readback_texts` parameter beside it is
the precedent for that shape.

### Where the handoff lives

The runtime, not the TUI (D5):

- the runtime already writes the `direct/<call_id>/` records, so holding "the
  direct-chat segments not yet handed off" keeps one fact under one owner;
- nothing is lost if the TUI crashes or restarts;
- "TUI only" means only the TUI gets a UI. Keeping the mechanism in the
  runtime is what lets a web surface adopt it later by drawing a view rather
  than reimplementing the protocol.

Mechanically: when `run_turn` handles a user turn with **no** `direct_target`,
it takes the session's pending handoff list, prepends the rendered block to
the user text, and clears the list. The TUI reads only
`pending_handoff_count` off `subagents.instances`, to draw a collapsed hint
line above the composer. That count is display, not authority.

## TUI

### Component and placement

A new `instanceChips.tsx` at the top of `ComposerPane`, above the
`QueuedMessages` element at `ui-tui/src/components/appLayout.tsx:235`. It
belongs to the same family as `QueuedMessages`, `stickyPrompt` and the
`bgTasks` counter - the status band above the composer - and follows that
layout convention rather than inventing one.

Rendering: `[Raven] [RC/refactor-auth*] [mir/scan]`. The first chip is a fixed
return-to-main chip; the rest follow the registry's `updatedAtMs`, most recent
first. `*` marks running. On overflow, truncate from the right and show `+N`;
never wrap, because `ComposerPane` is `flexShrink={0}` and a second row costs
a transcript row.

### State

A new `$directChat` store (nanostores, same family as `delegationStore` and
`uiStore`):

```
active:      { agent, handle } | null     null = the main agent
instances:   InstanceRow[]                the subagents.instances result
transcripts: Map<"agent/handle", Msg[]>   each instance's direct chat
scrollPos:   Map<"agent/handle", number>  each transcript's own scroll offset
```

The mode takeover swaps the `ChatStream` data source rather than mounting a
second transcript component: `virtualHistory` measures by `row.key`, so a
wholesale source swap is clean. Scroll position is remembered **per
instance** - without that, switching back jumps to the bottom, which is
painful in a long direct chat.

### Refresh

The registry is a JSON file with no change notification. Do not poll. The TUI
already receives `subagent.*` progress events
(`turnController.upsertSubagent`, `app/turnController.ts:1136`); refetch
`subagents.instances` on those, plus once at startup and once per session
switch.

### Greying

The server has exactly one slot, `_active_turns[session_key]`, so the rule is
symmetric and the client is only its mirror:

- a main-agent turn in flight -> direct mode is read-only, composer greyed,
  chips still switchable and history still scrollable;
- a direct turn in flight -> the main conversation is read-only too, greyed
  the same way.

The reason must be printed on the hint line under the composer ("Raven is
replying, sending is paused"), or the user sees only an unresponsive composer.

### Keys

Esc already carries five meanings in the TUI (voice bindings, queue editing,
terminal selection, cancel-turn, force-reset;
`app/useInputHandlers.ts:420-505`). Per D8, direct mode keeps Esc
unconditional:

- selection / queue editing / voice keep their existing priority and consume
  Esc first;
- in direct mode, Esc returns to the main agent whether or not a direct turn
  is in flight. One meaning, no branch, so "I thought I was leaving and it
  cancelled the run" cannot happen.

A consequence to surface: after Esc, an in-flight direct turn still holds
`_active_turns[session_key]`, so the **main composer is greyed too** until it
lands. Consistent with the symmetric rule above, but it needs its own hint
("Raven-Code is still replying; you can continue once it lands") or returning
to a greyed main composer is baffling.

There is no instance-scoped cancel. A direct chat *is* the session's turn, so
`Ctrl+C` (`turn.cancel`) already stops it, and `SubagentManager.chat` unwinds
that cleanly: the record is finished `cancelled` and the registry row follows.
A second key doing the same thing would differ only in which one the user
happened to press. The web surface keeps its own
`raven.subagents.instances.cancel`, because a spawn there is a background task
with no turn behind it.

> **Superseded** by `2026-08-16-concurrent-direct-chats-design.md` (D3). Once a
> direct chat runs on its own lane, `turn.cancel` -- which looks up the
> *session's* turn -- no longer reaches it, so the conclusion here ("no
> instance-scoped cancel") stands while its reason does not: a direct turn is
> now not cancellable at all, deliberately. The unwind described above still
> applies when the lane is torn down for another reason.

Other keys: `Ctrl+Left` / `Ctrl+Right` cycle chips -- over every instance, not
only the ones the strip had room for, so keyboard reach does not depend on
terminal width. Chips are clickable, with the handler on the `Box`: only `Box`
carries mouse props in this renderer, and a handler on the `Text` is silently
never called.

The no-mouse, no-chord fallback is a slash command rather than the picker
overlay this design first sketched: `/instance` lists the session's instances
and switches by the index it printed, by full name, or back with `main`. A
terminal that does not send `CSI 1;5D` leaves the chords dead, and one where
mouse reporting is off leaves the chips dead, so the fallback has to depend on
neither. Its listing filters `dag-node` rows exactly as the strip does, and the
index is over that filtered list, so what is shown as 2 is what 2 selects.

## Handoff protocol

### Trigger and consumption

The runtime keeps a pending list per session and **appends one entry as each
direct turn lands** - not on mode exit, because the user may enter and leave
repeatedly, or switch back to the main conversation without leaving at all.

When `run_turn` handles a user turn with no `direct_target`: take the whole
pending list, render it as a block before the user text, clear it. Take-and-
clear means one segment is never reported twice.

### Block shape

Pointers only, no transcript (D9). Aggregated by instance rather than
flattened per turn - five direct turns flattened is fifteen paths of noise:

```
[subagent direct chats since your last turn]
Raven-Code / refactor-auth  (cli)
  3 turns, 2026-08-13T04:12:07Z -> 04:19:41Z
  /root/.raven/workspace/sessions/<group>/<chat>/subagents/direct/Raven-Code/refactor-auth/
    20260813T041207Z-a1b2c3d4/{prompt.md,out.md}
    20260813T041502Z-e5f6a7b8/{prompt.md,out.md}
    20260813T041941Z-c9d0e1f2/{prompt.md,out.md}
mirothinker / scan  (openai)
  1 turn, 2026-08-13T04:21:03Z (running at handoff time)
  .../direct/mirothinker/scan/20260813T042103Z-33445566/prompt.md
```

- **Times are UTC ISO-8601**, not epoch milliseconds. This text is read by a
  model, which cannot map `1786...` onto the user's "just now".
- **Turns that have not landed are listed too**, tagged `running at handoff
  time`, with only `prompt.md` (there is no `out.md` yet). Because Esc can now
  leave mid-flight, this is a normal case, not an edge one.
- **Paths are absolute**, spelled out once per instance and abbreviated after.
  A read tool needs the absolute path; repeating it in full three times is
  waste.

### An invariant to keep

The block is prepended to user text, and it names files produced by a
sub-agent - nominally an untrusted-data path, which the codebase fences with
`wrap_untrusted` (`raven/security/trust.py`, used at
`backends/raven_loop.py:221`).

It does not need wrapping, because every byte in the block is minted by Raven:
agent names come from config, handles from the registry, call ids from
`make_call_id` (a timestamp plus hex), path segments through
`safe_path_segment`. This continues the existing invariant stated at
`raven/agent/subagent_history.py:24` - "A file name is never derived from a
sub-agent's output".

That reasoning must be written into the code as a comment. Without it, a later
change that adds a "last reply summary" field to the block breaks the
invariant silently.

## Testing

Per AGENTS.md 5.4, changes to an existing module extend that module's existing
test file rather than adding a new one.

Extended:

| File | What |
|---|---|
| `tests/test_subagent_manager.py` | `raven-loop` now writes registry rows (`agent="raven"`); direct chat and spawn serialize under `hold_handle` |
| `tests/test_subagent_history.py` | the `direct/<agent>/<handle>/<call_id>/` layout; an unlanded turn has only `prompt.md` |
| `tests/test_subagent_third_party.py` | `messages.json` replay for `openai`; `stateful` derived per kind |
| `tests/test_config_raven_sections.py` | `openai` plus `stateful: true` no longer raises |
| `tests/test_tui_rpc_subagents.py` | the four new `subagents.instance*` methods |
| `tests/test_spine_turn.py` | `TurnRequest.direct_target` |
| `tests/test_rpc_schema_match.py` | this fails automatically until `openrpc.json` matches the registered method set, so the schema is updated in the same change, not afterwards |
| `ui-tui/src/__tests__/createGatewayEventHandler.test.ts` | an event carrying `target` must not paint into the main transcript |

New:

- `tests/test_subagent_direct_chat.py` - `SubagentManager.chat()` resume paths
  for all three kinds
- `tests/test_subagent_handoff.py` - pending accumulation, take-and-clear,
  block rendering, UTC timestamps
- `ui-tui/src/__tests__/instanceChips.test.tsx` - chip ordering, overflow
  truncation, running marker
- `ui-tui/src/__tests__/directChatMode.test.ts` - mode switching, per-instance
  scroll memory, symmetric greying
- `tests/integration/test_direct_chat_smoke.py`

## Risks

Both real risks are in changed semantics, not new code.

1. **Widening `stateful`** changes the roster text that `spawn` and
   `run_subagent_dag` advertise to the model, and relaxes a config validator.
   Rollback is restoring `third_party_agent_meta` and
   `_reject_declared_stateful`; direct chat then degrades to unavailable for
   `openai` instances, leaving `cli` and `raven-loop` unaffected.
2. **The pending-handoff prepend in `run_turn`** runs on every ordinary user
   turn. An empty pending list must be a pure early return with no disk
   access, or this adds a filesystem hit to the main path.

## Follow-ups (not this change)

- Per-step progress for `cli` backends - which steps a run took, while it takes
  them. Reply streaming (above) does not provide it: it forwards the answer's
  text, not the run's tool calls, so a cli agent's roster row is still
  `no-progress` and `spawn` still reports only a final result.
- Reply streaming for `codex`, if it ever emits a partial event. Nothing on
  raven's side is missing; see "Streaming".
- Attachments in a direct chat: `SubagentManager.chat` takes `text` only, so
  `req.media` is dropped. No backend accepts anything but a prompt string
  today, so this is a contract change across all four, not a plumbing fix.
- A web UI surface over the same RPC and handoff mechanism.
- Direct chat with a DAG node, which needs a resume story for nodes first.
- Reporting files a direct chat modified, if a trustworthy source ever exists
  (per-backend tool-call reporting is the only candidate that is both
  accurate and cheap).
