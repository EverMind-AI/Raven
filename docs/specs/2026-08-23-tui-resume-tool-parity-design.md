# Tool-call parity across resume (TUI)

Status: proposed
Date: 2026-08-23

## Goal

A resumed session's tool-call rows must behave exactly as a live turn's do. Same
rows, same folds, same detail blocks, same clicks, same DAG graph. "Read a past
session" and "read the session you are in" become the same reading experience.

## Non-goals

- Live-only affordances: the running spinner, the elapsed-time counter, the
  in-flight `live` flag. They describe a turn in progress and mean nothing for a
  finished one.
- Improving on the live path. Where the live transport drops information (see
  *Parity is measured against live*), the resumed path drops it too. Adding it
  back is a separate change to both.
- The web UI. It already reads these fields.

## What happens today

`session.resume` returns stored messages; `toTranscriptMessages`
(`ui-tui/src/domain/messages.ts:41`) folds them into `Msg[]`. A `role: "tool"`
row becomes a **string** from `buildToolTrailLine`, pushed into `pending` and
attached to the next assistant message as `tools: string[]`.

So a resumed transcript has no `Episode`, no `EpisodeTool`, and no tool identity.
Every per-tool affordance is therefore absent, not broken:

| Affordance | Live | Resumed today |
|---|---|---|
| One row per call | yes | folded into one comma-joined string |
| Fold a stretch open/closed | yes (`seg:<id>`) | no id, no fold |
| Open a call's detail block | yes (`call:<id>`) | no id, no block |
| DAG graph under the call | yes (`tool.dag`) | nowhere to hang it |
| Click a DAG node to expand | yes | no panel to click |

The backend is not the problem. `raven/rpc/methods/session.py:228` documents that
three stored fields "ride along so a resumed transcript can be drawn with the
same detail as a live one", and names this exact TUI behaviour a "known
degradation". Verified on the wire and in storage:

- the resume payload carries `tool_call_id`, `name`, `context`, `timestamp`,
  `diff`, `duration_ms`, `reasoning_ms`, `reasoning_content`, and `tool_calls`
  flattened to `[{id, name, arguments}]` (`session.py:262-284`);
- `ui-tui/src/rpc/generated.ts:197-206` already declares them;
- a real stored session shows tool entries holding `tool_call_id`, `name`,
  `duration_ms`, `timestamp` and the full result `content`, and assistant entries
  holding `tool_calls` with complete `arguments` JSON.

`TranscriptRow` (`messages.ts:116`) declares six fields and ignores the rest.
**The whole gap is client-side.**

## Design

### A. Reconstruct episodes on resume

Parity does not require the live and resumed paths to share a builder. They
already share the **renderer**: `episodeView` is the only component that draws
tool rows, folds, detail blocks and clicks. The gap is that resume never produces
`Episode[]` for it. Feed it the same structures and identical interaction
follows by construction.

There is a working precedent. `foldDirectTurns`
(`ui-tui/src/domain/directEpisodes.ts:84`) already folds **stored** rows into
`kind: 'episodes'` messages with real `EpisodeTool` objects, for direct chats.
Direct chats have the fidelity this spec asks for; the main session does not.

So: extract the folding core out of `directEpisodes.ts` into a module both
callers use, with a thin adapter per input shape (`DirectTurn`, `TranscriptRow`).
One implementation, two callers -- which is what keeps parity from decaying into
two renderers maintained by hand.

Field mapping for the main session, all from data already on the wire:

| `EpisodeTool` | Source |
|---|---|
| `id` | `tool_calls[].id` |
| `name` | `tool_calls[].name` |
| `summary` | `callSubject(tool_calls[].arguments)` -- the same helper the direct path uses |
| `resultPreview` | the matching `role: "tool"` row's `text`, cut to live's own limit (see *Parity is measured against live*) |
| `durationMs` | that row's `duration_ms` (absent means unknown: draw no clock, never a zero) |
| `done` | `true` -- every resumed call has finished |
| `ok` | `false` when the stored result opens with a runtime failure marker, else `true` (see *Parity is measured against live*) |
| `dag` | hydrated, section B |
| `startedAt` | omitted -- it exists to drive a live elapsed timer |

Rows are matched to calls by `tool_call_id`, which is what the backend docstring
says the field is for.

**Fold ids are the clincher.** `foldStore`'s header states its keys are
"`call:<toolCallId>` and `seg:<firstToolCallId>` -- the transport's ids". Those
ids are in the resume payload, so a resumed transcript's folds use *the same
keys* a live one does. Parity here is not approximated; it is the same key space.

Preserved from today's `toTranscriptMessages`: the `origin` row's replacement
with the delegated-delivery line, and the long-message elision. That line is
drawn from the row's `delegated` field rather than its bare `origin`, so it
carries the same arrow, label and success/failure wording `subagent.delivered`
gives a live turn. A runtime-opened turn with no `delegated` (cron, sentinel,
heartbeat) keeps the older label-less sentence.

### B. The DAG graph

With real `EpisodeTool` rows, `tool.dag` has a home and the panel renders where
it always did.

One link is missing from the wire: which run a `run_subagent_dag` call started.
The stored result text contains it (`DAG run <id> finished:` plus a `Run dir:`
line), but that is prose authored by the tool.

**Decision: the backend derives it.** `session.resume` adds an optional
`dag_run_id` to the tool row, extracted from the stored content it already owns.
Derived at read time, so it works for every session already on disk without a
storage migration, and the parsing lives in the module that authors the string
rather than in a consumer that would be guessing.

The alternative -- parsing in the client -- was rejected: it puts a dependency on
prose formatting at the far end of the system, where nothing would notice the
format changing.

Hydration happens **during resume, before `setHistoryItems`**: collect the
`dag_run_id`s, fetch each through the existing `dag.get`, fold with
`foldDagSnapshot`, and attach to the matching `EpisodeTool`. Rendering stays a
pure function of state -- no render-time fetching, no second store.

A run whose directory was deleted attaches nothing, leaving the row without a
graph rather than an empty frame.

Node expansion needs no work. `DagPanel` takes `{run, t, width}` and no episode
identity (`dagPanel.tsx:219`); the expand key is `${runId}/${nodeId}` in a module
store; and `dag.get`'s snapshot carries `prompt_template` (`rpc/models.py:582`,
filled by `_reader.py:128` from `graph.json`). So resumed rows and boxes are
clickable, off the same keys as live ones.

### C. A DAG stretch opens by default

Two changes, both about the graph being visible without a click.

`foldStore` gains a third state. It stores open ids only, so "the reader closed
it" and "never touched" are indistinguishable; a content-driven default needs to
tell them apart, or a remount silently reopens what the reader just closed.

- `$folds` holds open **and** closed ids per scope; `isFoldOpen(scope, key,
  defaultOpen)` resolves unset to `defaultOpen`.
- `WorkSegment` takes `defaultOpen`, true when any tool in the stretch carries a
  `dag`. A stretch like "read skill ..., run subagent dag ..." therefore starts
  open, the DAG call gets its own row, and the graph renders in its normal place.
- `dagFor` also renders under a folded summary row, so folding by hand keeps the
  picture. The comment at `episodeView.tsx:265-269` forbidding this ("a folded
  stretch is one row, and a graph is not one row") is rewritten to say why the
  DAG call is the exception: it is the one tool whose result *is* a picture.

This applies to live and resumed transcripts alike, because both render through
`episodeView`.

## Parity is measured against live

The TUI has two live transports and they disagree about what a finished tool
call reports, so "what live does" has to be named before resume can match it.

- The **typed spine** (`raven/rpc/spine.py` through `chatStream.ts`) is what
  this repo ships. `ToolCompletePayload` (`rpc/models.py:410`) carries
  `result_preview`, `truncated`, `metadata` and `diff`, but `onToolComplete`
  forwards only the summary -- `recordToolComplete(tool_call_id, undefined,
  undefined, summary)` (`chatStream.ts:386`) -- dropping the error and the diff.
- The **legacy gateway handler** does forward `ev.payload.error`
  (`createGatewayEventHandler.ts:504`), and renders `ev.payload.inline_diff` as
  its own `kind: 'diff'` segment when the `inlineDiffs` setting is on (`:497`).

**The typed spine is the reference.** The legacy handler's diff branch cannot
fire against this repo's backend: nothing under `raven/` emits `inline_diff`,
and `_emit_inline_diff` -- which that branch's own comment names as the producer
-- does not exist there either. It is compatibility for a server this repo no
longer contains, not a live behaviour to hold a resumed transcript to.

What that settles:

- **A runtime failure marker is honoured.** The typed spine cannot report a
  failed call, but the runtime writes the failure into the stored text itself:
  `[interrupted] this call never returned` (`agent/loop/main.py:3907`) and
  `[failed] <text>` (`agent/subagent/backends/turn_rows.py:90`). A resumed row
  reads those, sets `ok: false`, and strips the marker from what it shows. This
  is not resume outrunning live -- the marker was already on screen inside the
  result text, so `ok: true` drew a row whose status contradicted its own body.
  `foldDirectTurns` already read `[failed]` this way; both adapters now share
  one list of markers instead of disagreeing about them.
- **`resultPreview` is cut to live's limit.** Live cuts server-side at
  `_TOOL_PREVIEW_MAX_CHARS = 4_000` (`agent/loop/main.py:352`) and the client
  appends ` (truncated)`. Storage keeps the whole result, so an uncut resumed
  row would show more than the turn it replays. The resume adapter applies the
  same limit and the same suffix, from one constant the live call site now reads
  too. The cut lives in the resume adapter rather than the shared fold core:
  `foldDirectTurns` feeds that core rows no live path cuts to this limit, and
  cutting there would truncate them for the first time.
- **`diff`, `added` and `removed` stay out.** All three reach an `EpisodeTool`
  only through `finalizeEpisodeTool`'s fifth argument; only
  `recordInlineDiffToolComplete` passes it, and only the dead legacy branch
  above calls that. So no live turn on this backend sets them: `EpisodeTool.diff`
  is written by one unreachable path and read by no renderer, and
  `episodeSummary`'s `(+N -M)` label cannot be produced. Restoring them on
  resume would show what no live turn shows. Reaching them is a change to the
  typed spine first and to both clients second -- it is not a resume gap, and
  this design does not close it.

Recording this so the next reader does not mistake any of it for an oversight in
this one.

## Files

| File | Change |
|---|---|
| `raven/rpc/methods/session.py` | derive `dag_run_id` on a DAG tool row |
| `raven/rpc/models.py` | `dag_run_id` on the resume tool-row model |
| `rpc-schema/openrpc.json` | regenerated |
| `ui-tui/src/rpc/generated.ts`, `ui/src/rpc/generated.ts` | regenerated |
| `ui-tui/src/domain/episodeFold.ts` (new) | the folding core, extracted |
| `ui-tui/src/domain/directEpisodes.ts` | adapter onto the core |
| `ui-tui/src/domain/messages.ts` | `TranscriptRow` reads the remaining fields; adapter onto the core |
| `ui-tui/src/app/useSessionLifecycle.ts` | hydrate DAG runs before `setHistoryItems` |
| `ui-tui/src/app/foldStore.ts` | tri-state |
| `ui-tui/src/components/episodeView.tsx` | `defaultOpen`; graph under a folded summary |
| `ui-tui/src/app/chatStream.ts` | reads the shared truncated-preview suffix instead of its own literal |
| `ui-tui/src/app/slash/commands/dag.ts` | refresh the history-attached run, and every run the transcript shows |
| `ui-tui/src/lib/dagGraphLayout.ts`, `dagGraphRender.ts` | carry edge identity, so an unrelated crossing is not drawn as a join |

An RPC field drags a regeneration chain behind it. This repo's review history is
explicit that a widened type fails loudly in an exhaustive `Record` and
**silently** where it is threaded through a spread, so both clients'
type-checkers must be run, not just the schema sync gate.

## Testing

- `episodeFold` core: one stretch, several stretches, a call with no result row,
  a result row with no matching call, a thought with no calls. Same suite drives
  both adapters, which is the point of extracting it.
- `messages`: a resumed payload produces `kind: 'episodes'` with the expected
  ids, summaries and durations; the `origin` row still becomes the delivered
  line; `duration_ms` absent draws no clock.
- Fold ids: the ids a resumed transcript mints for a given payload equal the ids
  the live path mints for the same calls. This is the parity assertion, and it
  should fail if either side changes its scheme.
- `foldStore`: unset resolves to `defaultOpen`; an explicit close survives a
  remount; `resetFolds` clears both sets.
- `episodeView`: a DAG stretch renders open by default; folded by hand it still
  draws the graph; a non-DAG stretch is unaffected.
- Resume hydration: a payload naming a run attaches the snapshot; a run whose dir
  is gone leaves the row without a graph.
- Python: `session.resume` emits `dag_run_id` for a DAG call and omits it
  otherwise, including for a stored result whose text names no run.

## Risks

This changes how **every** resumed tool renders, not only DAG calls. A resumed
transcript that has been a flat list of trail lines becomes a structured one, so
the regression surface is every session anyone reopens. Mitigated by the shared
core (the direct-chat path already exercises it) and by the fold-id parity test.

Rollback is per commit: the fold default, the episode reconstruction and the DAG
hydration are independent, and reverting any one leaves the others working.
