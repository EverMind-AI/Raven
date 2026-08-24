# A running DAG node's live stream, and its trace box (TUI)

Status: proposed
Date: 2026-08-23

## Goal

A running node in the DAG panel says what its sub-agent is doing *now*, without
a click: one line under the node's row carrying the tail of its message stream,
refreshed as the stream moves, so a reader can see the run working rather than
infer it from a spinner. Clicking the row opens a fixed-height box holding the
node's conversation trace, drawn by the same component the direct-chat view
draws messages with. Clicking again returns to the stream line.

## Non-goals

- **A push channel.** No `dag.*` event carries per-step content, and this does
  not add one. See *A*.
- **Scrolling or folding inside the box.** Eight rows is too small to fold, and
  a nested scroll region inside the virtualized transcript would need its own
  focus and would take the mouse wheel from the transcript. *D* is the way out
  instead.
- **Failure state and `diff` in the box.** The main transcript renders neither
  (the resume-parity design's *Parity is measured against live* records why), and
  a DAG node showing more than a live turn does is a parity break in the other
  direction.
- **The web UI.** Its node panel already fetches `dag.node` on click; a live tail
  there is the same idea against a different renderer, and its own change.

## What happens today

Clicking a node row opens its **prompt template** -- `NodePrompt`
(`ui-tui/src/components/dagPanel.tsx:43`), read from `node.promptTemplate`, which
the client already holds from the tool call's arguments. No fetch, and nothing
about what the node did.

Nothing in the TUI shows a node's trace at all. `/dag <node>` fetches
`dag.node`, which *returns* the trace, and `formatDagNodeDetail`
(`ui-tui/src/lib/dagStatus.ts:151`) prints `prompt` and `output` and drops
`messages` on the floor.

## Design

### A. The data is already live; nothing new goes on the wire

`dag.node` returns `messages` for a node in flight. When nothing has reached
disk yet -- the normal state of a node being watched -- `_with_messages` falls
back to the activity this very process is collecting, keyed by run and node
(`raven/rpc/methods/dag.py:126`, published at
`raven/agent/subagent_dag/runner.py:720`).

Two lanes fill it, at different granularities:

| Lane | Publishes | Granularity |
|---|---|---|
| ACP | `acp_agent.py:208` | every answer chunk, thought chunk and tool-call frame |
| raven's own loop | `raven_loop.py:329` | once per iteration of the tool loop |
| cli | nothing | no per-step visibility exists in that lane |

The ACP collector republishes its **whole** transcript on every update, which is
what makes a poll correct rather than lossy: each response is a complete
snapshot, so a reader keeps no state and cannot miss a frame it was not awake
for. `activity.py` says so directly -- the transcript is republished "rather than
once at the end" because "the live index is how a panel watches the run".

So this feature is client-side. It mirrors `useDirectStepPoll`
(`ui-tui/src/app/useDirectStepPoll.ts:42`), which already polls exactly this kind
of snapshot for the direct-chat view at 400ms.

**A new hook, `useDagNodePoll`.** Mounted from `useMainApp`. Each tick it calls
`dag.node` for every node it needs:

- every node of a live run whose status is `running` -- for its stream line;
- every node whose key is in `$dagOpenNodes` -- for its trace box, whether or
  not it is still running.

`DAG_NODE_POLL_MS = 500` in `config/limits.ts`, a little slower than the direct
poll because a tick is one call per node rather than one call total. The store is
cleared on session reset, alongside the other per-transcript view state -- traces
belong to the conversation on screen, and a resumed one is a different
conversation.

`run_subagent_dag` returns as soon as it launches the run, so a node routinely
keeps running well after the turn that started it has ended -- `idle()` clears
the live `dagRuns` at exactly that boundary, while the run's own process, and
the activity `activity.collecting` registered for it, are still alive. So the
poll reads past the turn: it merges `turnStore`'s live runs with the runs
pinned onto their tool rows in the transcript, the live one winning where both
name a run -- the same source `/dag` already falls back to after a real
cross-process resume.

A pinned run's displayed status is a frozen copy, though, never revised once
the turn that pinned it closes -- so the poll cannot stop on it, or a node
kept polling past its own turn would be polled forever. It stops on the
response instead: once `dag.node` reports anything other than `pending` or
`running`, that node is recorded as settled and never read again, whatever
its displayed status still says. A node already displayed as finished needs
neither of those: it is fetched once when opened and then left alone, since a
stopped target is read at most once regardless of what that read returns. A
node displayed as running has no such floor if its run dir has been pruned,
though, so a read that keeps throwing is itself the signal --
`DAG_TRACE_READ_FAILURE_CAP` consecutive failures settle the node exactly as
a terminal status would.

### B. The stream line

One row, directly beneath its node's row, for exactly as long as the node's
status is `running`. Tied to status rather than to "has content arrived" so the
panel's height does not change the moment the first chunk lands.

```
  2 ● codex-acp: audit the journal writer
      ⠹ write leaves a truncated object behind, so the reader has to
```

The content is the **tail of the flattened stream**, cut to the row's width: the
last line's worth of whatever the agent is producing. Not a structured
one-step-per-line ticker -- the point is that the text visibly moves, which is
what reads as work in progress.

Flattening rule, applied to the wire messages in order: a message contributes
its `reasoning_content`, then its `text`, then one label per entry in
`tool_calls` -- `formatToolCall(name, callSubject(arguments))`, the pair the
transcript's own tool rows are built from (`text.ts:236`, `episodeFold.ts:53`);
a `role: "tool"` message contributes its result text. Contributions are joined
by ` · ` and every whitespace run inside one collapses to a single space, so the
result is one line no matter how the agent formatted it.

**Walked from the end, not built and then sliced.** A transcript is capped at 400
messages server-side and this runs twice a second: accumulate backwards until
the width budget is covered, then cut. Cost is proportional to the row width,
not to the transcript.

Cutting uses display cells, not characters -- `clipToWidth`
(`ui-tui/src/lib/text.ts:65`) is the existing forward version and already handles
CJK and emoji through the fork's `stringWidth`. This needs its mirror,
`clipToWidthFromEnd`, in the same file.

**Liveness when the text is static.** During a long `Bash` call nothing is
published for tens of seconds and a frozen tail reads as a hung run. So the line
carries a spinner glyph at its head, ticking on its own interval, independent of
the poll. Before the first update arrives the line is the spinner and a muted
`working...`: true for an ACP node that has not spoken yet and equally true for a
cli node that never will, which is the honest thing to say without the client
having to know which lane it is watching.

### C. The trace box

Clicking the row -- or the node's box in the graph, which shares the same toggle
key (`dagOpenNodes.ts:31`) -- replaces the stream line with a fixed-height box.
The two are one slot in two states, which is what makes the second click read as
"go back".

```
  2 ● codex-acp: audit the journal writer
    ╭──────────────────────────────────────────────────╮
    │ n3f8a2 · codex-acp · 14 msgs                     │
    │ ▸ Read raven/agent/acp/journal.py                │
    │   340 lines · 1.2s                               │
    │                                                  │
    │   The journal writes one frame per line, so a    │
    │   partial write leaves a truncated object behind │
    │   and the reader has to skip it.                 │
    │                                                  │
    │ ▸ Bash npm test -w ui-tui                   ● 4s │
    │ ↑ 6 earlier messages — /dag 2                    │
    ╰──────────────────────────────────────────────────╯
```

**What is expandable changes.** `dagNodeToggleKey` (`dagOpenNodes.ts:31`) returns
`null` for a node with no `promptTemplate`, because the template was the only
thing a click could reveal. Now there is a trace, which exists whether or not the
arguments carried a template -- so the rule becomes: expandable unless the node
is `pending` *and* has no template, which is the one case where a box would open
onto nothing. `dagSpanToggleKey` reads the same helper, so a node's box in the
graph and its row still cannot disagree.

`borderStyle="round"`, matching the graph's own node boxes. Geometry:

| Part | Rows |
|---|---|
| border | 2 |
| header: node id, sub-agent, count of wire messages, `→ outputFile` when known | 1 |
| trace: `TRACE_ROWS = 8`, blank-padded when the trace is shorter | 8 |
| footer: `↑ N earlier messages — /dag <ordinal>`, else `/dag <ordinal> for the full trace` | 1 |

`DAG_TRACE_BOX_ROWS = 12` outer. `height` on a bordered Box *is* the outer
height -- the fork sets a Yoga border (`packages/hermes-ink/src/ink/styles.ts:704`)
-- so 12 leaves exactly the ten content rows above.

**Always 12, never 11 or 13**, and both halves matter. The footer is drawn even
when nothing was cut, and the trace is blank-padded when it is shorter than eight
rows. A box that grew as the run produced messages would shove every row below it
several times a second; worse, *E* could then not state the panel's height without
measuring the trace, which would make the height model depend on the fold and the
fold depend on the height model.

The header exists for the node id: it is the one thing the collapsed row does not
show. The footer names the *ordinal* instead, because that is what a reader with
a mouse already has in front of them -- the graph prints it in every box and at
the head of every row, and `/dag` takes either.

**Rendering is the direct-chat renderer, not a lookalike.**
`toTranscriptMessages` (`ui-tui/src/domain/messages.ts:54`) already maps this
exact wire shape -- `dag.node`'s `messages` and `session.resume`'s rows are the
same model -- into `Msg[]`, and `MessageLine` dispatches `kind: 'episodes'` to
the episode view (`ui-tui/src/components/messageLine.tsx:101`). The box renders
`MessageLine` per message. Same glyph vocabulary, same tool rows, same prose,
because it is the same code.

**Tail-fitting is a pure function.** Which messages to draw is decided before
rendering, by measuring candidates from the end with `estimateRows`
(`ui-tui/src/lib/text.ts:305`) until `TRACE_ROWS` is covered. `height` plus
`overflow="hidden"` (supported by the fork --
`packages/hermes-ink/src/ink/styles.ts:376`) is a guard for the last partial
message, not the mechanism.

The alternative -- render everything and let `justifyContent="flex-end"` push the
overflow off the top -- was rejected: it depends on Yoga clipping children at a
negative offset, which is unverified here, and it would draw a 400-message
transcript to show eight rows of it.

**Fallback.** When the fetch yields nothing usable -- pruned run dir, a run older
than the reader, a server error -- the box falls back to today's prompt-template
block. The pre-existing behaviour is the floor; a reader never gets an empty
frame. While the first fetch is in flight the box shows the template too, so
expanding is instant and the trace swaps in when it lands.

### D. `/dag <node>` prints the trace

Without this, eight rows is the only view of a trace that exists, and a
60-message run has 50 messages reachable by no means at all. That is what makes
the fixed box safe rather than lossy.

`formatDagNodeDetail` gains the messages it is already handed: prompt, then the
trace, then the output. The transcript is a surface that already scrolls, and
the command already makes exactly this call.

### E. Height and the virtualized transcript

`dagPanelRows` (`ui-tui/src/lib/virtualHeights.ts:72`) is the panel's height
model and currently counts one row per node, so it needs both new terms: `+1` for
a running node's stream line, and `+DAG_TRACE_BOX_ROWS` (12) for an open node's
box -- a constant, per the geometry table in *C*.

The stream line's term is derivable from the run alone, since status is on the
node. The box's is not -- `$dagOpenNodes` is a module store and
`estimatedMsgHeight` is a pure function of a `Msg`. So the open set is threaded
in as an option from `useMainApp:333`, which is a hook and can read the store.

It must **not** go into `messageHeightKey`: that key is also a message's
identity (`useMainApp:260`), and identity that changes on a click would make a
toggle look like a new row. `dagSig` does gain a count of running nodes, which is
a property of the run and changes only when the run does.

Getting this wrong is not a visible jump: heights are measured at unmount
(`useVirtualHistory.ts:420`), so a panel on screen renders at its true height
either way, and the estimate only positions rows that have never been mounted.
The term is added so that a panel restored above the viewport is not off by ten
rows.

## Which nodes get a stream

Driven by **data, not by backend kind**: every running node gets a line, and it
shows whatever its lane published.

Gating on ACP specifically would need the roster. A DAG node carries `subagent`,
`depends_on` and `instance` on the wire and no backend at all, so the panel would
have to fetch `subagents.list` and match by name -- and it would then hide the
stream for raven's own loop, which publishes a real transcript
(`raven_loop.py:329`). The cli lane publishes nothing and so shows the spinner
and `working...`, which is what it should show.

The feature is named for ACP because ACP is the lane where it is vivid -- chunk
granularity makes the tail actually move.

## Files

| File | Change |
|---|---|
| `ui-tui/src/app/useDagNodePoll.ts` (new) | poll `dag.node` for running and open nodes, live or pinned |
| `ui-tui/src/app/dagNodeStore.ts` (new) | `runId/nodeId -> { messages, settled }` |
| `ui-tui/src/lib/dagStream.ts` (new) | flatten-from-the-end, and the trace tail-fit |
| `ui-tui/src/components/dagNodeTrace.tsx` (new) | the stream line and the box |
| `ui-tui/src/components/dagPanel.tsx` | render the new slot; `NodePrompt` becomes the fallback |
| `ui-tui/src/lib/text.ts` | `clipToWidthFromEnd` |
| `ui-tui/src/lib/dagStatus.ts` | `formatDagNodeDetail` prints the trace |
| `ui-tui/src/lib/virtualHeights.ts` | stream-line and box terms; running count in `dagSig` |
| `ui-tui/src/app/useMainApp.ts` | mount the poll; thread the open set and pinned runs into it |
| `ui-tui/src/domain/dagRun.ts` | export `dagRunsFromHistory`, now shared by the poll and `/dag` |
| `ui-tui/src/app/slash/commands/dag.ts` | import `dagRunsFromHistory` instead of defining it |
| `ui-tui/src/config/limits.ts` | `DAG_NODE_POLL_MS`, `TRACE_ROWS` |
| `ui-tui/CONTEXT.md` | define **Stream line** and **Trace box** |

No backend change, no RPC schema change, no client regeneration: `dag.node`
already returns everything this reads.

## Testing

- `dagStream`: a tail shorter than the width; longer; a CJK tail cut on a
  double-width cell; newlines collapsed; a transcript of 400 messages walked in
  time proportional to the width rather than the transcript.
- Tail-fit: eight rows of budget against messages of known height picks the last
  ones; one message taller than the whole budget still renders and reports the
  cut.
- `dagNodeTrace`: a running node draws one line; the same node open draws the
  box; a finished node draws neither until opened; a node with no fetch yet draws
  the template.
- Toggle: click the row, then the node's box in the graph -- the same slot
  toggles, which is the invariant `dagNodeToggleKey` exists to hold.
- `useDagNodePoll`: polls a running node; polls an open finished node once and
  then stops; polls nothing when no run is live; stops on unmount.
- `virtualHeights`: the estimate for a panel with one running and one open node
  matches what the panel actually renders. This is the assertion that fails if
  either side changes alone.
- `formatDagNodeDetail`: a detail with messages prints them; one without prints
  what it prints today.

## Risks

**Poll volume.** A tick is one `dag.node` per running-or-open node, each of which
is three file reads server-side, and each response carries the node's whole
transcript. Bounded by a DAG's parallelism -- a handful of nodes -- and it is a
local socket. If it does prove heavy the fix is a narrower request (`tail_chars`,
returning the flattened tail instead of the messages), which is a backend change
this design deliberately does not make up front.

**Expanding a finished node becomes a round-trip** where today it is instant.
Masked by drawing the template immediately, but it is a real change to a path
that was purely local.

**Every DAG panel gains rows.** A run with four nodes running has four more rows
than it does today. That is the feature, but it is worth saying plainly: this
makes a live DAG panel taller.

Rollback is per commit: the stream line, the trace box and the `/dag` change are
independent, and reverting any one leaves the others working.
