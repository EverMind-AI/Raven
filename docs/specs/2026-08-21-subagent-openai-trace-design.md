# Sub-agent OpenAI-API trace - design

Date: 2026-08-21
Status: designed
Base: to be cut from `main`. AGENTS.md 2.2 - the base is confirmed with the
maintainer before the branch is cut, not chosen here.

Companion to `docs/specs/2026-08-21-subagent-trace-extracted-memory-design.md`.
That spec excludes the `openai` kind from trace-extracted memory because the
kind leaves no trace to extract from. This spec removes the reason. Whether that
spec then widens its scope is its own decision, taken there.

Reverses one decision in
`ui-webui/docs/specs/2026-07-20-openai-subagent-miromind-design.md` (decision 2,
2026-07-20): "the verbose step-by-step `reasoning_steps` trace is not
surfaced". It was deferred, not rejected.

## Goal

An `openai` sub-agent's Instance Log holds a prompt and an answer and nothing
between them. Give it the steps, in the rows an `acp` run already leaves, so one
renderer draws both and one extractor reads both.

## Why this is needed

Two facts, both measured rather than assumed.

**The host drops the middle.** `openai_api.py` never calls
`activity.note_transcript` or `activity.note_usage`. So `activity.transcript`
stays empty, `add_turn_to_instance_log` passes `messages=None`
(`subagent_history.py:149`), and `append_turn` writes exactly two rows - the
prompt and the answer (`instance_log.py:121-128`). The record directory gets no
`transcript.jsonl`, which is written only when the activity carries one
(`subagent_history.py:232`), and `meta.json` carries no token cost for the same
reason.

**The endpoint sends the middle.** It arrives in a provider extension field the
host has never read. The `cli` kind is in the same position for a different
reason - it has no per-step visibility at all - and stays out of scope.

## Measured

Probed 2026-08-21 against `mirothinker-1-7-deepresearch` at
`https://api.miromind.ai/v1`, one trivial prompt and one that forces tool use.

Buffered (`stream: false`), the research prompt:

| Where | What |
| --- | --- |
| top level | `id`, `object`, `model`, `created`, `choices`, `search_results`, `usage` |
| `choices[0].message` | `role`, `content`, `reasoning_steps`, `agent_summary` |
| `reasoning_steps[i]` | `{type, <payload>}`; the payload key is the type name, except `thinking`, whose key is `thought` |
| `thinking` | `thought`: str |
| `web_search` | dict `{search_keywords: [str], search_results: [{title, url, snippet}]}` |
| `fetch_url_content` | dict `{url, snippet}`, where `snippet` is a JSON *string* holding `{error, extracted_info, success, tokens_used, url}` |
| `search_results` | 24 entries of `{title, url, snippet}` |
| `usage` | `prompt_tokens`, `completion_tokens`, `total_tokens`, `completion_tokens_details.reasoning_tokens`, `num_search_queries` |

Nine steps: `thinking` x5, `web_search` x3, `fetch_url_content` x1.

Streaming (`stream: true`), 305 frames, and the shape is *not* the same:

| Fact | Consequence |
| --- | --- |
| delta keys: `content` x171, `reasoning_steps` x109, `agent_summary` x22, `role` x1 | four independent streams in one frame sequence |
| `reasoning_steps` is always a list of exactly one step | no batching to unpick |
| `thinking` arrives as token fragments (106 frames, e.g. `"\nUser"`, `" asks"`) | must be accumulated across frames |
| action steps arrive whole, one complete dict per frame | no accumulation |
| `search_results` and `usage` appear only on the final frame | read at end of stream, not per step |
| `agent_summary` is a fragmented duplicate of `content` | dropped |

Three of the six documented step types were not triggered:
`execute_python`, `execute_command`, `tool_call`. The reader is therefore
generic over the type name rather than switching on a closed set.

## Non-goals

- **Trust fencing.** `wrap_untrusted` is not applied. It exists on exactly one
  path today (`raven_loop.py:322`); the `acp` lane has none, and fencing one
  transport and not the other would be worse than fencing neither. Recorded
  under Risks.
- **The transcript cap.** `activity.py:262` keeps the *first* 400 rows, so a
  long run loses its tail. Unchanged here: the cap is shared with `acp`, and
  changing it is a decision about both.
- **Memory extraction.** The companion spec owns it.
- **The `cli` kind.** No per-step visibility to read.
- **The DAG lane's missing answer row.** Pre-existing and shared with `cli` and
  `builtin`; see Risks.
- **Responses API, `mcp_servers`, `response_format`, `-mini` preset.** Still
  deferred, as in the 2026-07-20 spec.

## Design

### 1. Turn Rows: one row builder, two producers

New `raven/agent/subagent/backends/turn_rows.py`. It owns the row shape and
nothing else: an ordered event list in, provider-shaped message rows out.

Event vocabulary, transport-neutral: `say(text)`, `thought(text, at)`,
`call(id, name, arguments_json, at)`, `result(id, text, ok, at)`.

`rows(events, *, in_flight_answer=None)` reproduces the algorithm
`AcpCollector.messages()` runs today, verbatim:

- a `call` emits one assistant row wearing the thought that preceded it:
  `{role, content: <narration>, tool_calls: [{id, type: "function", function:
  {name, arguments}}], timestamp, reasoning_content?}`
- a `result` emits `{role: "tool", tool_call_id, content, timestamp}`, with
  `content` prefixed `[failed] ` when not ok
- a thought left over at the end emits `{role: "assistant", content: "",
  reasoning_content, timestamp}`
- the final answer is **not** a row. The record keeps it, and the reader appends
  it as the Closing Message. `in_flight_answer` appends the partial answer
  instead, for a live read that has no record to append from.

`say` exists for the `acp` lane, which narrates between steps. The OpenAI lane
emits none, so `content` is `""` on every call row it produces - the same value
the `acp` lane writes for a turn that did not narrate.

The module must not import `acp_dialects`: a `call` event carries a name and an
`arguments_json` string, not a `ToolCall`. `AcpCollector.messages()` becomes a
thin call into it.

This is the whole point of the change. The value of matching the `acp` row shape
is that an `openai` instance reads identically to an `acp` one, and two
implementations of one shape lose that at the first divergent fix.

### 2. Step Dialect: the OpenAI step reader

New `raven/agent/subagent/openai_steps.py`. It turns `reasoning_steps` into
Turn Rows events and knows nothing about rows.

Two entry points for the two response shapes:

- `feed_steps(steps: list[dict])` - the buffered list, in one call
- `feed_delta(step: dict)` - one streamed step, called per frame

and `events()` for the accumulated result.

Accumulation lives here because only here is it needed: consecutive `thinking`
fragments append to an open thought, and any non-`thinking` step closes it.
Buffered input runs the same accumulator with each step arriving whole, so both
paths converge on one event list - which is the property the tests pin.

Payload reading is generic:

- `thinking` -> a `thought` event carrying `thought`
- a *measured* action type -> a `call` event plus a `result` event. One step's
  payload holds both halves, so the halves go to their own rows. No key is
  renamed and none is added; the result half is simply peeled off the arguments
  rather than written into both rows:

  | Type | `arguments` | result row text | ok |
  | --- | --- | --- | --- |
  | `web_search` | `{search_keywords: [...]}` | `search_results`, JSON-encoded | always true - the payload reports no status |
  | `fetch_url_content` | `{url: ...}` | `snippet` unwrapped to its `extracted_info` | `success is not False` and `error` empty |

  Unwrapping `snippet` - a JSON string inside the payload - is the ACP Dialect's
  "output with the transport's wrapping removed", not a rename.
- an *unmeasured* type -> a `call` event only, carrying the whole payload as
  arguments, and **no** result row. Which key holds the result is not knowable
  without seeing one, and inventing a split would put a guess in an audit
  record. An unpaired call row already renders: it is what a call still running
  looks like. The first real `execute_python` step is what turns it into a
  measured type.

Call ids are synthesised (`mi-1`, `mi-2`, ...). The endpoint sends none, and the
id exists only to pair a call row with its result row.

### 3. No translation at write time, on either lane

The record carries the transport's own name. Presentation is recoverable from
provenance; provenance is not recoverable from presentation, and the file is
what an extractor reads.

`openai`: the step type name, verbatim - `web_search`, `fetch_url_content`,
`execute_python`, `execute_command`, `tool_call`, and any type added later.
Argument keys are the payload's own, per the table in section 2.

`acp`: the transport's own name at the finest granularity the transport
provides.

| Adapter | Name stored |
| --- | --- |
| claude-agent-acp | `_meta.claudeCode.toolName` verbatim (`Bash`, `Read`, `Glob`, `Grep`, `LS`, `Write`, `Edit`, `NotebookEdit`, `BashOutput`, `WebFetch`, `WebSearch`, `Task`) |
| codex-acp, and any adapter without that field | the spec's `kind`, verbatim (`execute`, `read`, `search`, ...) - the protocol's own name, not a translation of it |
| neither present | `tool_call`, as today. The no-name case, not a mapping. |

This also settles the arguments, because today they share one key: `arguments_json()`
looks up `ARGUMENT_KEY.get(self.name)` (`base.py:132`) and renames the subject
onto raven's key, dropping the adapter's own spelling of it (`base.py:118-130`).
With the transport's name stored, that lookup misses and the arguments fall
through to `rawInput` verbatim. One change removes both translations, which is
why they move together in section 4.

### 4. Normalisation at the read boundary, and the wire is unchanged

`_KIND_TO_TOOL`, `claude_code.py`'s `_TOOL_NAMES` and `ARGUMENT_KEY` move from
write time to read time. They are not deleted and not copied: they move.

The read boundary is `raven/rpc/methods/instances.py`. It normalizes a stored
row on the way out - the name through the moved tables, then the subject
promoted onto raven's key as `arguments_json()` does today - so
`subagents.instance.history` emits **byte-identical rows to the ones it emits
now**.

The promotion reads the *stored arguments*, not the `acp`-only notion of a
call's subject. It looks for the subject under the tool's own key first
(`ARGUMENT_KEY[normalized_name]`), then under the generic `SUBJECT_KEYS` order,
then - last - under any string the payload happens to carry, and re-keys it onto
the tool's key, dropping the field it came from by key, never by value. All
three have to miss for a row to pass through untouched, which is what makes the
same helper correct for both lanes:

| Stored row | Promotion | Result |
| --- | --- | --- |
| `acp` `Bash` + `{command, description}` | name -> `exec`, `command` already the key | today's wire, unchanged |
| `openai` `fetch_url_content` + `{url, ...}` | `url` found, but the name has no key | verbatim; `url` still renders |
| `openai` `web_search` + `{search_keywords: [...], search_results: [...]}` | no subject found - both values are lists | verbatim; the row renders with a verb and no subject |

The last line is the accepted cost of storing the payload verbatim. Adding a
derived string key to make it render was considered and rejected: the record
says what the endpoint sent.

Three details of the write path have to survive the move, or the wire changes
for `acp` rows in ways nobody asked for. An empty value (`None`, `""`, `{}`,
`[]`) is dropped rather than carried, because the write path never sent one. A
subject that reaches the frame outside `rawInput` - the spec's `locations`, or
the adapter's `title` - is stored under the literal key `argument`, which is
where `arguments_json()` already puts it today, and `SUBJECT_KEYS` therefore
carries `argument` so the read boundary can lift it onto the tool's key. And the
generic first-string sweep is the last of the three lookups above rather than
being dropped, since `argument()` has one today. That sweep cannot misfire on an
`openai` row: `web_search` is the only step type that is also an `ARGUMENT_KEY`
entry, and both values in its payload are lists, so there is no string for the
sweep to find.

**The first of two places the wire is not byte-identical.** Today's write-time promotion
scans a fixed key order and takes the first hit, so a call carrying two subject
candidates is re-keyed with the wrong one: `grep` with
`{"pattern": "TODO", "path": "src/"}` reaches a client as
`{"pattern": "src/"}`, the search pattern destroyed. Preferring the tool's own
key fixes that, which means the wire changes for exactly those calls - to the
value that was always meant to be there. Measured before deciding: across 219
stored tool calls and 220 `rawInput` objects in 13 ACP frame journals, no call
carries two candidates, so no observed traffic changes at all. Carrying the
defect forward was the alternative, and this helper becomes its sole
implementation once section 3's tables move.

**The second is a claude tool the old table did not list.** `claude_code.py`
checked the adapter's `toolName` against twelve entries and, on a miss, fell
through to the `kind` mapping - so `TodoWrite`, `ExitPlanMode`, `MultiEdit`,
`SlashCommand` and every `mcp__*` tool reached a client as `tool_call` or, worse,
as `exec`. The record now keeps the name the adapter sent, and the read boundary
has no entry to map it back, so the wire carries `TodoWrite`. This one the read
boundary cannot undo: `kind` is not stored, so the old answer is unrecoverable.
It is kept deliberately. A todo-list write labelled `exec` was wrong, and
`ui-tui`'s `humanize` gives an unlisted name a real label rather than a wrong
verb. Adding any of these to `RAVEN_NAME` later needs no write-path change.

That is the design's cheapest property and its strongest test: three front ends
read this method (`ui-tui` via `episodeSummary.ts`'s verb table and `QUOTED`,
`ui-webui` via `MessageBubble.tsx:400` dispatching renderers *by tool name*,
and `ui` via the transcript renderer), and none of them changes. No
`TranscriptToolCall` field is added, so `raven/rpc/models.py`,
`rpc-schema/openrpc.json` and the generated client types are all untouched.

Exposing the transport's own name on the wire is a separate, additive change,
for a client that wants to show it. Nothing wants it yet.

Three call sites read these stored rows, and all three apply the helper or the
panels disagree about what a run called:

| Call site | Reads |
| --- | --- |
| `instances.py::_log_turns` (`:205`) | the Instance Log file, *and* the in-flight rows - `_live_turns` (`:196`) calls it, so one edit covers both |
| `dag.py::_with_messages` (`:139`) | a DAG node's `transcript.jsonl`, or its live activity |
| `subagent.py` (`:421`) | one call record's `transcript.jsonl`, for `subagent.context` |

The last two hand their rows to `_map_to_wire` (`session.py:213`), and the
helper must **not** live inside it: that mapper also serves the main session
transcript, whose calls are the host's own and already raven-named. Running an
ACP name table over them is a corruption waiting for the first collision.
Normalise the rows at each site, before the mapper.

### 5. Wiring in the openai backend

`openai_api.py` gains a reader per call and publishes three things:

- buffered path: `feed_steps(message["reasoning_steps"])` after the response
  parses, then `activity.note_transcript(rows(reader.events()))`
- streaming path: the delta loop in `_stream_chat` gains a `reasoning_steps`
  branch - it reads `content`, `reasoning_content` and `reasoning` today and has
  never looked at `reasoning_steps` (`openai_api.py:125-138`) - calling
  `feed_delta(step)` per frame and republishing `note_transcript` as it goes, so
  the live view fills in while the run works. Ambient `note_transcript` is enough: unlike
  the ACP read loop, which runs in a task created when the *connection* opened
  and so needs a named run (`acp_agent.py:207`), the stream is consumed inside
  `run()` and sees the right ContextVar.
- both paths: `activity.note_usage(usage)`, which also gives an `openai` call
  the token cost in `meta.json` that it has never had.

The final answer is not published as a row; it reaches the log as it does today,
through `output` on the record.

The existing `reasoning_content` / `reasoning` read stays exactly as it is. It
is a *different* field serving a different purpose - the answer's fallback when
a reasoning model puts the answer only there - and an endpoint may send either
field, both, or neither. Measured: mirothinker's stream carries
`reasoning_steps` and no `reasoning_content`, so for this endpoint the two never
overlap.

### 6. Failure behaviour

Every publish is best-effort, as everything on this path already is: an audit
trail must not fail the run it describes.

A step whose payload reports failure - `success: false`, or a non-empty `error`
in `fetch_url_content`'s unwrapped snippet - produces a result row with the
`[failed] ` prefix, the same signal `acp` uses (`acp_agent.py:341`).

A malformed step, an unparsable nested `snippet`, or a payload key that is
missing entirely yields the rows it can and drops what it cannot read. A turn
that answered is not failed by a step the reader could not parse.

### 7. What stays out of the rows

`search_results` (the top-level citation list) and `num_search_queries` are not
captured. Not for want of a home - `RunActivity.as_meta` (`activity.py:93-113`)
is a closed field set, so each would need a field of its own - but because both
are now redundant. The top-level list is the union of the per-step
`search_results` that the result rows already carry, and the count is the number
of `web_search` call rows. Storing a rollup of rows beside the rows invites the
two to disagree.

Token counts *are* captured, through the existing `activity.note_usage`, which
reads `prompt_tokens` and `completion_tokens` (`activity.py:41-42`). An `openai`
call has never had them in `meta.json`.

`agent_summary` is dropped - measured equal to `content`.

## Testing

New files, per AGENTS.md 5.1:

- `tests/test_subagent_turn_rows.py` - the row builder. The two assertions that
  pin the shape today (`tests/test_subagent_acp.py:432-454`: the in-flight
  partial, and "the answer belongs to out.md, not the transcript") move here and
  are asserted against the module directly.
- `tests/test_subagent_openai_steps.py` - the step reader, with fixtures built
  from the payloads captured in Measured: the 9-step buffered response and the
  305-frame stream. The central assertion is that **both produce the same event
  list**, and therefore the same rows.

Changed:

- `tests/test_subagent_acp.py` - one end-to-end assertion stays, so a refactor
  that breaks the `acp` lane still fails. `:405-427`'s
  `name == "read_file"` becomes the transport's own name, with a sibling
  assertion that the *wire* still says `read_file`.
- `tests/test_rpc_instances.py` - the read boundary emits unchanged rows for a
  stored row in the new shape. This is the regression test for all three front
  ends at once.

## Domain terms

`CONTEXT.md` gains two terms and one existing term is corrected.

**Turn Rows** (`raven/agent/subagent/backends/turn_rows.py`): the
provider-shaped message rows one delegated turn contributes to the Instance Log,
built from a transport-neutral event list. Both the ACP collector and the
OpenAI Step Dialect produce that list, which is what makes an `openai`
instance's conversation read identically to an `acp` one.
_Avoid_: confusing them with **Live rows** - same shape, different source.

**Step Dialect** (`raven/agent/subagent/openai_steps.py`): how one
OpenAI-compatible endpoint's `reasoning_steps` extension is read into events -
the step's own tool name, its payload, and its result with the transport's
wrapping removed. Sibling to **ACP Dialect**, for a transport that reports its
steps in a response field instead of a notification.

**Tool Vocabulary** (`raven/agent/subagent/tool_vocabulary.py`): raven's own
tool names (`exec`, `read_file`, ...), and the mapping into them applied when a
delegated run's rows go on the wire. A record carries the transport's name
because provenance is not recoverable from presentation; the wire carries
raven's because every renderer's verb table is keyed by it, and the main session
log stores the host's own calls under those same names.
_Avoid_: applying it at write time - that is what this replaced.

**ACP Dialect** is corrected: it no longer produces "a raven tool name
(`exec`, `read_file`, ...)". It produces the adapter's own name, and the
mapping into raven's vocabulary happens at the read boundary. The `_Avoid_` on
reading `title` as the tool name stands.

## Delivery

Two merge requests off one spec, in order:

1. **The read boundary and the `acp` rename.** Moves `_KIND_TO_TOOL`,
   `_TOOL_NAMES` and `ARGUMENT_KEY` to `instances.py`, stores the transport's
   own name, and proves the wire is unchanged. Self-contained: it ships a
   behaviour-preserving change to one lane.
2. **Turn Rows, the Step Dialect and the openai wiring.** The new capability,
   landing on a read boundary that already speaks both vocabularies.

Split this way because one MR would change the `acp` lane's stored data shape
*and* add an `openai` feature, giving a reviewer two unrelated things to hold at
once. The order matters: doing 2 first would land `openai` rows that the read
boundary does not yet normalize.

## Risks

**Untrusted content in a file an LLM will read.** A `fetch_url_content` result
is a fetched web page, and it now lands in the Instance Log unfenced. Today
nothing reads that file but a renderer; the companion spec's extractor will.
Fencing is deliberately not added here (Non-goals) because the `acp` lane is in
the same position and the fence's real consumer is in that spec. Named so the
decision is taken there rather than inherited silently.

**A trace that outgrows its cap.** A trivial probe already produced 9 steps and
109 step frames. A real research run can exceed 400 rows, and the cap keeps the
first 400, so what a reader loses is the end of the run. Unchanged here, stated
so the first report of a truncated tail is recognised rather than investigated.

**Undocumented step types.** Three of six were never observed, so their payload
shape is inferred from the documented type-named-key rule. The generic path
means an unexpected shape degrades to a row with an odd `arguments` blob, not to
a crash - but the first real `execute_python` step is worth looking at.

**The DAG lane still loses the answer row.** `_add_node_to_instance_log`
(`subagent_dag/runner.py:565`) passes no `output`, and the answer falls back to
`activity.closing`, which only the `acp` lane sets (`acp_agent.py:667`). So an
`openai`, `cli` or `builtin` node run through a DAG writes its steps and its
prompt but no answer. Pre-existing, wider than this change, and not fixed here.
An `openai` DAG node will therefore have a *more* complete trace than before and
still be missing its last row.
