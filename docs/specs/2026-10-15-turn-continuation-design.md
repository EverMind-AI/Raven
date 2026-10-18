# Turn Continuation: resuming a failed turn from its recorded model request

Date: 2026-10-15. Status: design approved; Delivery item 1 (the full failure text) implemented, item 2 (Turn Continuation) pending.

## Problem

A turn that fails on a model call leaves the reader with two dead ends.

1. **The error cannot be read in full.** The web client draws a failed turn as one row,
   `label + firstErrLine(detail, 140)` (`NoteView`, `ui-web/src/features/transcript/TranscriptPage.tsx:1152`),
   with the rest only in a hover title. That title is itself cut: the runtime bounds
   every failure text at 200 characters before it leaves the process
   (`TURN_FAILURE_TEXT_MAX`, `raven/spine/events.py:91`; `LLM_ERROR_DETAIL_MAX`,
   `raven/providers/base.py:83`), and the session marker's `turn_ended.reason` is
   bounded the same way. For an OpenRouter 429 this drops exactly the fields that say
   what happened (`provider_name`, `limit_source`, `remedy_hint`). The trace keeps the
   whole text (the failed `llm.call` span's `status.message`), but nothing in the web client
   reads it.
2. **Retry restarts the turn.** The row's retry action is `send(rt.lastAsk)`
   (`ui-web/src/state/session/stages.ts:260`): the question is sent again as a new
   turn, so the transcript carries it twice, the context is assembled from scratch, and
   the iteration count starts over. It exists only while the web client that saw the failure
   stays open; a failed turn replayed from the session has no retry at all.

## Goals

- **G1** A failed-turn row expands to the full failure text, scrubbed and bounded,
  both live and when the session is replayed.
- **G2** Retry resumes the failed turn at the model call that failed: the first
  request of the resumed run carries that call's message list unchanged, no user
  message is added, and the completed tool work is kept.
- **G3** A failure that was resumed stays visible where it happened, as a quiet note
  with the original error, and never reaches the model again.
- **G4** When the recording cannot seed a resume, retry sends a plain go-on message in
  the configured language ("Go on" / "继续", from `config.language`).

## Non-goals

- The terminal TUI (`ui-tui/`). It has no retry today and keeps none.
- Cancelled turns. A person stopped them; they are not resumable.
- The 200-character failure texts. Chat-channel replies, cron job records and logs keep
  them as they are.
- Resuming anything but the session's last turn.
- ACP replay (`raven/acp/replay.py`). A resumed marker keeps replaying as the failure
  text it is today.

## Terms

**Turn Continuation** (added to `CONTEXT.md` with this change). A turn with no inbound
message that resumes the session's last failed turn from that turn's last recorded
model request: the `llm.input` artifact of the turn's trace. It is a turn of its own
(its own `turn_id` and lifecycle events) that the transcript draws as the rest of the
failed one.
_Avoid_: "retry" unqualified (`CONTEXT.md` already reserves it for the Model-Error
Ladder and Empty-Response Recovery); "continuation" unqualified (the DAG runner's node
continuations, `max_continuations`).

**Continuation seed.** The message list read from that artifact, resolved to full
messages.

The web client's button keeps its existing label, `gui.retry`.

## Why the trace, not the session

The session file records the conversation; the trace records each request as the
provider received it. In an inspected failed turn,
the failed `llm.call` span's `llm.input` artifact (`audit.artifact.v2`) held the system
prompt as assembled for that call, all 24 messages exactly as sent (a pasted screenshot
inline as a data URI, reasoning content, a mid-turn user message, three tool results),
the 34 tool definitions and the generation parameters. Re-sending those messages is the
failed call, with no context assembly, memory recall or skill selection run again.

Tracing is on by default (`TracingConfig.enabled = True`; `raven/tracing/config.py`
reads a missing key as on) and off only through `RAVEN_TRACING` or
`tracing.enabled = false`. Its writes are best effort: a write error is swallowed
(`raven/tracing/store.py`), and the trace directory may be cleaned by hand. A
continuation therefore checks the recording before it uses it and falls back when the
check fails (G4).

## Design

### 1. `turn.continue` RPC

Params: `{session_key}`. Result: `{status, turn_id?, reason?}`.

| status | meaning | client action |
|---|---|---|
| `continued` | a continuation turn was submitted; `turn_id` names it | none; the stream arrives |
| `fallback` | the recording cannot seed one; `reason` is `no_trace_id`, `no_model_call`, `trace_unreadable` or `tail_mismatch` | send the go-on text as an ordinary message |
| `refused` | nothing may run; `reason` is `busy`, `not_failed` or `already_continued` | show a toast; send nothing |

`not_failed` covers every session whose last entry is not a failed-turn marker,
including one where a newer message followed the failure, so a retry clicked on an old
row can never send a go-on message into a later conversation.

Eligibility is decided twice. The handler decides it to answer the client. The lane
decides it again before the run, and that answer is the authoritative one: a lane runs
one turn of a session at a time, so a second click that passed the handler's check
finds the marker already resumed, and its turn ends without output and writes nothing.

The method sits beside `turn.send` and `turn.cancel` in `raven/rpc/methods/turn.py`,
refuses a busy session through the existing `is_session_busy`, and is declared in
`rpc-schema/openrpc.json` with its mirrors (see "Contract surface"). It opens the turn
on the wire the way `turn.send` does, since the spine leaves `message.start` to the
submitting handler: it binds the lane's turn slot, so `turn.cancel` stops a
continuation like any sent turn, and emits `message.start` with `continued: true`
(section 6).

### 2. Loading and checking the seed

A new module beside the turn path (working name `raven/agent/loop/continuation.py`)
turns a session into either a seed or a reason, in this order:

1. **The failure.** The session's last entry is an assistant marker with
   `turn_ended.status == "failed"` and no `turn_ended.resumed`. Otherwise `refused`
   (`not_failed` / `already_continued`).
2. **The trace.** Walk back to the failed turn's opening user entry (the nearest
   `role == "user"` that is not a mid-turn message) and read its `trace_id` (the Turn
   Trace Mark). None: `fallback` / `no_trace_id`.
3. **The last model request.** Read that trace's spans from the span logs, newest file
   first (`raven/trajectory/store.py:span_log_paths` lists the active log and its
   archives), and take the last `llm.call` span. None: `fallback` / `no_model_call`.
4. **The artifact.** Its `llm.input.artifact_path` must resolve inside
   `<tracing state dir>/logs/audit-artifacts/` (a path that escapes is refused, as
   `raven/trajectory/replay.py:_artifact_path` refuses one for bundles), parse as JSON,
   and resolve through `raven/tracing/artifact_v2.py:resolve_payload` with no missing
   message blob. Otherwise `fallback` / `trace_unreadable`.
5. **The tail.** The seed's messages after its opening user message are compared with
   the session's entries of the failed turn between its opening user entry and the
   marker, after dropping what `_save_broken_turn` synthesizes on the way out (the
   `[interrupted]` tool results and the `partial` assistant text, section 4). The two
   must have the same sequence of roles and tool-call ids. Contents are not compared:
   the session may hold a capped copy of a long tool result. A difference means the
   turn failed after its last model call (a tool died with it) and the recording is
   stale: `fallback` / `tail_mismatch`.

### 3. Running a continuation

`TurnRequest` (`raven/spine/turn.py`) gains a continuation flag; such a request carries
no text, has origin `USER`, and is submitted only onto an idle lane. The turn path
handles it inside the existing turn machinery rather than in a parallel copy of it.

What runs, in order:

1. The in-lane eligibility check (section 1), then the seed load (section 2). A marker
   already resumed means another click won: the turn ends without output and writes
   nothing, and the winning turn's stream reaches every window. A seed that stopped
   loading in between (the trace was removed) fails the turn before anything is
   written, so the marker stays unresumed and the next retry falls back.
2. The marker gets `turn_ended.resumed = true` and the session is saved, before the
   first model call.
3. Turn setup that does not depend on an inbound message: tool context, the message
   tool's `start_turn`, the `ask_user` context, the turn tier and attachment scopes,
   `TurnSpend`.
4. The model binding of an ordinary turn: the session's model, else the router over the
   failed turn's question text, else the default. The provider, key and routing are
   the current ones, so a key or model changed after the failure is in force; the
   artifact's recorded `generation` block is not replayed.
5. The attempt machinery with the seed as its list: `persist_from` is the seed's
   length, `turn_start_idx` is the seed's opening user message. Tools are offered per
   iteration as for any turn, not taken from the artifact.
6. The counters continue from the failed turn. The iteration count and the episode
   index start at the number of model calls the failed turn completed (its assistant
   entries in the seed), so the budget is the turn's, not renewed by each retry, and
   live step rows do not reuse the failed turn's indexes, which the client keys fold
   state by. `_run_agent_loop` gains a starting offset for this.
7. Everything after the attempt is the ordinary path: the answerless-turn exits and
   their rescue (`_save_broken_turn`, which writes a new marker that can be continued
   in turn), the `after_send` chain, `_save_turn`, feedback dispatch.

What does not run: the `before_user_inbound` hooks, slash commands, token-triggered
consolidation (the next ordinary turn runs it), the personalization flow, the playbook
turn message, skill selection, context assembly, `planning.prepare`, and filing a
question.

### 4. Session records and the model's history

- `_save_broken_turn` (`raven/agent/loop/turn_path.py:2828`) tags the assistant text it
  saves from the stream with `partial: true`.
- A resumed marker keeps its place; the continuation's messages are appended after it.
- One helper decides what the model may see, used by both `Session.get_history`
  (`raven/session/manager.py:281`) and the Memory role's `candidate_messages`
  (`raven/agent/harness/memory.py:89`): a marker with `turn_ended.resumed`, and the
  `partial` entries directly before it, are left out. An unresumed marker is kept, as
  today: the model reads that a turn failed.
- `session.history` (`raven/rpc/methods/session.py`) already forwards `turn_ended`
  whole, so the new keys reach the web client with no change there.

### 5. The full failure text

- The 200-character texts stay. Alongside them, a failed turn carries `full_detail`:
  the unbounded failure text, scrubbed twice -- with the scrubber
  `raven/providers/call_record.py` already applies to response bodies (made public as
  `scrub_credentials`), then with `raven.security.redact.redact`, which knows the
  shapes a crash message carries (a key in a URL, userinfo, `password=`, vendor
  tokens) -- and capped at `call_record.MAX_BODY_CHARS` (8,000) rather than a new number.
  The scrubbers read a window of at most 32,000 characters for a crash. A private key
  without closing armour is scrubbed through the end of that window, since a prior
  replacement can move a cut fragment inside the output cap. Call records use both
  scrubbers before their own body cap. Authorization whitespace is consumed without
  backtracking, and JWT matching starts only at token boundaries -- matching `eyJ`
  anywhere cost 21 seconds of CPU on a 256,000-character body of it, and a token
  glued to a word (`/verify-eyJ...`) is what that trade gives up. Known truncation
  is passed into the scrubber so incomplete JWTs, vendor tokens, URL passwords and
  labelled credential tails are removed even without their normal minimum length
  or closing delimiter. Each tail pattern's search starts behind the run of its
  own value's characters that ends the text, the label's gap and scheme word, and
  room for the label's name, rather than at the start: the value runs to the end
  of the input, so retrying the pattern at every position makes label-shaped text
  cost its length squared -- 64,000 characters of `pwd=` ending in a comma take
  10.8 seconds, and a regex search for the run's start costs the same on a long
  gap, 32 seconds for 31,000 spaces inside a failing turn. That start is found by
  matching the reversed text step by step, which reads a gap of any width once,
  so the search finds exactly what a whole-text search finds. Ordinary
  unlabelled diagnostic text is preserved.
  The long text is the scrubbed one; the 200-character texts keep the wording they had,
  unscrubbed, so a credential inside a short crash message makes the opened text differ
  from the row only by its redaction.
- **The runner builds it, the lane carries it.** `raven/spine` is the frozen kernel:
  it imports no other raven package and holds a line budget
  (`tests/test_kernel_budget.py`), so it can neither scrub nor reach `call_record`.
  A new agent-layer module, `raven/agent/loop/failure_text.py`, builds the text, and
  the runner sets it as a `full_detail` attribute on the exception it lets out:
  `AgentLoop.run_turn`'s failure arm, which every agent turn passes, and before that
  `_process_message`'s arms, which also write the marker. The lane copies a string
  `full_detail` from the exception onto `TurnFailed.full_detail`. An exception that
  arrives without one (a runner other than the agent loop) carries none, and its row
  shows the bounded text, as today.
- **A model call the loop gave up on.** `AnswerlessTurnError` (kernel) gains an
  optional `full_detail` keyword. When the Model-Error Ladder gives up
  (`turn_path.py`, the branch that sets `error_detail`), the loop fills
  `LoopOutcome.error_full` from the failed response: its content unbounded, then the
  body its call record kept (`call_record.from_exception` already holds the provider's
  response body, scrubbed and capped). `_process_message` raises
  `AnswerlessTurnError(outcome.error, full_detail=outcome.error_full)`. A provider
  exception that escapes the loop (the stream ladder re-raises it) is covered by the
  generic rule above: its message, then the body its client library kept
  (`call_record.exception_body` -- the response text, else an HTTP error's parsed
  `exc.body`; litellm reduces an OpenAI-style error body to one line in `str(exc)` and
  keeps the rest there). `call_record.from_exception` reads the same body, so a
  non-streamed error response's call record keeps it too.
  The turn path and outer runner share the computed account, including an absent
  one, so a failure is not scanned twice. Attachment and lane reads use the base
  exception's instance storage; a foreign read-only property or throwing getter
  is never consulted.
- It travels three ways: `TurnFailed.full_detail` (`raven/spine/events.py`), the
  `error` event (`raven/rpc/spine.py` passes it to `emit_error`, a new optional
  `ErrorEventPayload.full_detail`), and the marker (`turn_ended.full_detail`, written
  by `_save_broken_turn`). All three carry it only when it says more than the bounded
  text: a failure the 200 characters already hold whole gets no second copy and no
  toggle.
- Who sees it: the RPC clients of this runtime (the web client, the TUI). A chat channel's
  reply keeps quoting the bounded text, as `TurnFailed.reported` already decides.

### 6. The web client (`ui-web`)

- **Expand.** `NoteView` gains an expand toggle when the full text is longer than what
  the row shows or spans several lines: a focusable button with `aria-expanded`,
  revealing the full text in a wrapped, selectable monospace block. The state is local
  to the row and never persisted. `note()` / `noteRow()` take the full text as an
  option; the live `error` handler passes `p.full_detail`, the replay passes
  `m.turn_ended.full_detail`. A marker written before this change has none; its row
  opens to the bounded `reason` it kept, when that is longer than the row's line.
  Quiet runtime notices use the neutral "Show details" label for the same expansion.
- **Retry.** A failed-turn row is no longer removed when its action is clicked; the
  handler calls `turn.continue`. `continued`: nothing more here (the next point turns
  the row). `fallback`, or an error from the call (an older Raven gateway without
  the method): the row loses its action and the web client `send()`s the go-on text,
  `t('gui.turn.go_on')`, which follows `config.language` because the web client's
  language is the Raven gateway's. `refused`: the row loses its action and a toast says
  `t('gui.turn.continue_refused')`. The row stays in both cases because the replay
  will show it there: an unresumed marker followed by whatever came next.
- **Replay.** An unresumed failed marker that is the session's last entry gets the same
  retry action. A resumed marker draws as a quiet note, `gui.transcript.resumed`, with
  its `full_detail` expandable and no retry.
- **The continuation's start.** `message.start` gains `continued: true` for a
  continuation turn. Every window that receives it turns the trailing failed-turn row
  into the resumed note (so the window that clicked, any other window on the session
  and a later reload all show the same row), then enters the streaming state without
  drawing a question and without `advanceTurn()`: like a delegated result re-entering
  the conversation, this turn has no user entry, and counting it would put the live
  `wsCurrentTurn()` ahead of the one a reload computes.
- **Strings** (`i18n/messages.json`, read by the web client directly and regenerated into
  the TUI's copy). The web client's key gate (`ui-web/scripts/gates/i18n-keys.test.mjs`) wants a
  key drawn under `features/transcript/` in the `transcript` namespace; the two
  `gui.turn.*` keys are said only by `state/session/`, a domain of its own to that
  gate. The unused `gui.err.detail` pair cannot be reused for the same
  reason.

  | key | en | zh |
  |---|---|---|
  | `gui.transcript.error_expand` | Show full error | 展开完整错误 |
  | `gui.transcript.notice_expand` | Show details | 展开详情 |
  | `gui.transcript.error_collapse` | Hide | 收起 |
  | `gui.transcript.resumed` | Interrupted here, then continued | 此处曾中断，已重试 |
  | `gui.turn.go_on` | Go on | 继续 |
  | `gui.turn.continue_refused` | This turn can no longer be continued | 这一轮已无法续跑 |

### Contract surface

`rpc-schema/openrpc.json` is the source; each addition below is mirrored in
`raven/rpc/models.py` and both generated clients (`ui-web/src/rpc/generated.ts` via
`scripts/gen-rpc-client.mjs`, `ui-tui/src/rpc/generated.ts` via
`scripts/gen-rpc-types.mjs`). The hand-written `ui-tui/src/gatewayTypes.ts` needs no
entry: it declares only `message` for the error event, and its drift test
(`gatewayTypesDrift.test.ts`) covers the model-picker payloads alone.

- method `turn.continue` with its params and result;
- `ErrorEventPayload.full_detail` (optional);
- `MessageStartPayload.continued` (optional, default false);
- `TranscriptTurnEnded.full_detail` and `TranscriptTurnEnded.resumed` (optional).

Every addition is optional, so a client that does not know a field behaves as today.

## Testing

Backend, `uv run --all-extras pytest`:

- **Seed.** A recorded v2 `llm.input` with `$msg` references as a fixture: the first
  request of the continuation carries the recorded messages unchanged. Each fallback
  reason from its own fixture: no `trace_id`, no `llm.call` span, a missing blob, an
  artifact path outside the artifacts directory, and a session tail one tool call
  longer than the seed.
- **Eligibility.** Each refusal reason; two concurrent submissions of which exactly one
  runs.
- **Records.** The marker is marked resumed before the first model call; new messages
  follow it; the iteration and episode counters continue from the failed turn; a
  continuation that fails writes a new marker that is itself continuable.
- **History.** A resumed marker and the `partial` text before it are absent from both
  history views; an unresumed marker is present.
- **Full text.** The bounded `error` is unchanged; `full_detail` is scrubbed, capped,
  and present for a raised model error, a crash and an answerless turn.

Web client, `ui-web` vitest: the expand toggle appears only when there is more to show and
keeps `aria-expanded` right; each `turn.continue` outcome; the two replay renderings;
`message.start` with `continued` draws no question and does not advance the turn.

End to end: a real `raven web` under an isolated `RAVEN_HOME`, against a local
OpenAI-compatible stub that fails the Nth call with a 429, answers afterwards and
records every request body. Retry in the browser, then check that the continuation's
first request body equals the failed one, that no user message was added, and that
after a reload the note is the resumed one and the marker is absent from the next
request. One ordinary conversation on the live default model confirms the unchanged
paths.

Gates: `make lint check-source-language`, the RPC contract checks (`ui rpc contract`,
the TUI checks for `gatewayTypes.ts`), the `ui-web` build and type check.

## Delivery

Two pull requests, in this order, because the repository lands one feature per squash
commit:

1. **The full failure text (G1).** Section 5, the expand half of section 6, the
   `full_detail` fields of the contract surface, and their tests. Useful on its own:
   every failure row becomes readable.
2. **Turn Continuation (G2 to G4).** Sections 1 to 4, the rest of section 6, the
   remaining contract fields, the `CONTEXT.md` term, and the end-to-end check. It
   builds on the first: a resumed note expands to the `full_detail` its marker keeps.

## Compatibility and risks

- Sessions written before this change: markers without `full_detail` or `resumed`
  render as today. Such a failure can still be continued when its trace exists.
- A failure that is deterministic (a request the provider will always refuse) fails
  again on continue and leaves a new row; nothing retries on its own.
- The seed is read from local files Raven wrote, through the same confinement replay
  uses for bundles; it is as large as the failed request, inline images included.
