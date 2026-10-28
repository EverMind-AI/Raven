# The trajectory page as an agent-first waterfall

The trajectory view answers one question: what happened, in what order, and
who was doing it. Today it answers the first two well and the third not at
all. It draws every entry as a block in a single horizontal cost bar, and it
shows one Sub-agent Scope at a time behind a tab strip. A reader can see that
a sub-agent exists, and can switch to its tab, but can never see the thing
that matters in a multi-agent system: **what ran at the same time as what**.

This document replaces the bar and the row list with a waterfall grouped by
agent, then by turn, on one shared, gap-compressed time axis. The details
pane, the RPC layer and the block renderers stay.

## Why the grouping is agent first

A sub-agent does not run inside its dispatcher's turn. Measured on a recorded
session (`traces/bundles/trace-1a0c4b9cedc-3f27d2af/spans.jsonl`):

| span | start | end | length |
|---|---|---|---|
| host `session.turn` | 16:08:26.588 | 16:08:47.395 | 20.8s |
| `subagent.run` | 16:08:42.260 | 16:10:55.504 | 133.2s |

The host turn ended 128 seconds before the sub-agent did. The sub-agent's
report then arrives as a *new* host turn (`TrajectoryEntry.dispatched_by_subagent_scope`
names the scope whose report started it). Nothing waits; nothing blocks.

A turn is therefore a bounded execution window, and an agent is a lifetime
that spans several of them. Grouping by turn first forces a 133-second child
into a 21-second container and makes every bar wrong. Grouping by agent first
matches the lifetimes: the agent's whole run on one line, its turns stacked
inside, its spans inside those.

## The shape of the page

```
┌ status line (index progress, truncation, faults) ─────────────────┐
├ overview: one mini lane per agent ────────────────────────────────┤
├ ruler: shared axis, idle gaps collapsed and labelled ─────────────┤
├ agents ────────────────────────────────────────┬ details pane ────┤
│ ▾ main  · model · 3 turns · tokens            │ selected span's  │
│     Turn 1  ▓▓░▓▓▓                            │ blocks: overview │
│       ├ llm.call    ███                        │ input / output  │
│       ├ spawn A     ⇢                          │ timing / links  │
│       └ memory.store  ░░   (outside any turn)  │                  │
│     Turn 2  ⇠ report from A   ▓▓▓              │                  │
│ ▾ A · ACP · 3 turns                            │                  │
│     Turn 1  ⇡ from main  ██████                │                  │
└────────────────────────────────────────────────┴─────────────────┘
```

Three levels, one vertical axis of rows and one horizontal axis of time.

- **Agent** — a group with a header row: name, model or transport, turn count,
  tokens, and a lifetime bar showing every moment it was active.
- **Turn** — a row inside the group: the turn's own segmented bar, its input
  preview, its duration and tokens. Collapsed by default; expanding reveals
  its spans.
- **Span** — a row inside the turn, indented by `parent_span_id` depth. Its
  own bar, its own duration.

Work an agent did outside any turn (a `memory.store` that outlived its turn,
a `plugin.load` after the report) is a row directly under the agent, not
inside a turn. It is real and it has a time; it just has no turn.

## What each visual piece means

**Bars are activity, not cost.** Bar geometry uses `operation_start` /
`operation_end` — the span's real extent. `charged_ms` stays out of the
geometry entirely: it exists to attribute a duration to one owner so costs do
not double count, and a waterfall that hides a child because its time was
charged to the parent would be lying about who was busy.

**A turn's bar is a composite.** Its children's intervals are swept once and
each sub-interval is painted by priority `failure > sub-agent > model > tool >
waiting > input`, the way `dsh-trace-waterfall`'s `compositeSegments` does.
That is what makes a collapsed turn readable: model time, tool time and
sub-agent time are visible without expanding it.

**The sub-agent turn family is a second hue.** Sub-agent turns use their own
colour ramp so a reader can tell, at a glance, whether a bar belongs to the
host or to a worker. Within an agent, colours keep the existing
`--trajectory-c-*` kinds.

**Links are drawn at the dispatch instant.** A dispatch is a vertical rule at
the x of the spawning span, running from the parent row down to the child
agent's header. A report is a vertical rule at the child's end, running up to
the host turn that it started. These two rules are the only cross-agent
decoration; everything else is position.

## The time axis

`createTimeAxis` in the reference implementation, reimplemented here:

1. Take every span's real interval, excluding turn and agent summaries.
2. Merge them into a set of occupied intervals. **All agents contribute**, not
   just the host — otherwise the 128 seconds a sub-agent spent working look
   like an idle gap and get compressed away, which would hide the exact
   overlap the page exists to show.
3. Between occupied intervals, any gap longer than 60s becomes a fixed-width
   break labelled with its real length (`12.4m idle`).
4. `project(t)` and `unproject(x)` interpolate across the resulting piecewise
   map, so every bar, the ruler, the overview and hit testing share it.

Zoom and pan operate on the projected domain, as the current bar's viewport
already does.

## Per-agent turn ordinals

`turn_number` on the wire is a single ordinal over every `session.turn` span in
the loaded set, so sub-agent turns consume numbers in the host's sequence. The
page must not print it raw. Turns are grouped by `turn_span_id` within an agent
and ordered by their earliest `event_time`; the displayed ordinal is that
position. The raw numbers stay in the details pane as wire values.

## Loading a large session

The snapshot walk is streamed. Every page `trajectory.list` returns (200 rows by
default, 500 at most) goes on screen as it lands, whether or not a picture is
already up -- a refresh over a session the reader has seen before used to wait
for the whole snapshot, and that wait is the one large files actually feel. A
walk restarted after its cursor expired is the exception: its pages belong to a
different snapshot, and half of one is not a picture.

The tree and the axis grow with those pages rather than being rebuilt over the
whole session each time. `store.grownFrom` is the store's word that the last
change appended to the list (the length before the append) or rewrote it (-1);
`WaterfallIndex` and `AxisIndex` keep what they already built and refresh only
what a page touched. A rewrite starts them over. `incremental.test.ts` and
`axis.test.ts` hold each grown structure equal to its one-shot build, so the
asymptotics are a property a test states rather than a claim in a comment.

## What this costs in the store

`store.ts` currently derives `visible` by discarding every entry whose
`subagent_scope` is not the selected one (`derived()`, `own !== scope`), and
`select()` only accepts an entry present in `visibleIndex`. Both assumptions
are load-bearing for the tab-per-scope page and both are wrong for this one:
the waterfall has to place every agent's entries at once, and a click on any of
them has to select it.

So the store grows a second derived set — every entry the page may show
(hidden excluded unless revealed), indexed by entry id — and `select()` accepts
an id from either index. `visible` keeps its meaning for the tab strip and the
details pane's first-block preference; it stops being the only door.

Agent naming reuses `scopes.ts`'s existing title logic so a group header, a tab
and the details pane name a worker the same way.

## The decisions this design takes

These were open, and are now settled here rather than left to the builder.

- **The details pane stays.** `detailStore` is 1400 lines of block loading,
  cursors, width clamping, narrow-screen overlay and Escape ordering. The pane
  moves from beside a list to beside a waterfall; nothing inside it changes.
- **Sub-agent Scope tabs become a filter, not a mode.** The waterfall shows
  every agent at once, so switching tabs no longer changes what can be seen.
  The strip keeps its job as a locator: clicking a tab scrolls to that agent
  and selects its door entry.
- **The duration threshold filter is removed.** It existed so the cost bar
  would not drown in sub-millisecond blocks. The waterfall answers the same
  problem with zoom and with turn-level collapsing, and a filter that can
  silently hide a span from the only view of it is worse than no filter. The
  `minChargedMs` preference, its detent scale and its popover go with it.
- **Hidden entries stay hidden, with the existing switch.** `meta.hidden` is
  the index's verdict, not a display preference; the toggle that reveals them
  carries over unchanged.
- **Turn rows are collapsed by default; agent groups are expanded.** A
  session's turn count is large and its agent count is small. Agent groups
  collapse too, for the DAG case where ten workers run at once.
- **Rows are windowed.** `EntryList` already renders a window around the
  viewport (`ROW_HEIGHT`, `OVERSCAN`, `windowOf`). The waterfall keeps that
  approach; a 387-step session must not mount 387 rows × 3 levels.
- **Agents are a flat list.** A group per scope, ordered by first event, the
  host first. If a worker ever dispatches another, the child group is a
  sibling and the dispatch rule points at it; group nesting is not attempted.
- **A turn's label comes from its own rows.** The turn's input preview is the
  `preview` of its `turn_start` entry, and its duration is the span of its rows
  on the axis. No extra fetch is introduced for a turn summary.

## What is removed

`DurationBar.tsx` and its canvas painter, `geometry.ts`'s block/slot model
(anchor, dense-block gathering, `expand`), and the bucket popover. The parts of
that arithmetic worth keeping — fit/zoom/pan on a viewport, and the hit test —
are re-expressed against the new axis in a new module rather than carried
over. `EntryList.tsx` is replaced by the agent/turn/span row renderer, but its
window arithmetic and its keyboard behaviour are the starting point.

## Non-goals

- No change to `raven/trajectory/` or to the `trajectory.*` RPC surface. The
  page is a mirror of what the index already holds; every field it needs
  (`subagent_scope`, `opens_subagent_scope`, `dispatched_by_subagent_scope`,
  `turn_span_id`, `parent_span_id`, `operation_start`, `operation_end`,
  `duration_ms`, `meta.hidden`) is on the wire today.
- No live streaming animation inside a running span. A running span's bar ends
  at `now` and extends on the clock tick the store already provides.
- No changes to the chat view or to the Chat/Trace toggle.

## Risks

- **Cross-trace ancestry.** A sub-agent's spans live in their own trace, so
  `parent_span_id` does not reach the dispatching span. Agent membership comes
  from `subagent_scope`; the dispatch rule comes from the spawing row's own
  interval plus the child's `scope_id`. Depth inside an agent is still
  `parent_span_id`.
- **Entries with no resolvable turn.** `turn_span_id` is nullable and the index
  reports `turn_unknown` in `integrity`. These rows must land somewhere
  truthful — under the agent, outside any turn — rather than being dropped.
- **Very long sessions.** 150 LLM calls and 387 steps is ordinary; 5000 is
  possible. Windowing plus memoised per-turn composite segments (recomputed
  only for the visible rows and only when the clock ticks on a live span).
- **The three colour ramps.** Host turns, sub-agent turns and span kinds now
  coexist on one surface. Contrast has to be checked in both themes before the
  palette is accepted, not after.

## Acceptance

1. A session with a sub-agent whose run outlives its dispatching turn renders
   that sub-agent's bar extending past the host turn's end, across the idle
   break, to its own end.
2. The host turn that a report started carries a visible origin marker naming
   the agent.
3. The idle break is labelled with the real elapsed time and is not compressed
   while any agent is active.
4. Turn ordinals are contiguous within each agent, starting at 1.
5. Work outside a turn appears under its agent, not inside the nearest turn.
6. The details pane opens the same blocks for the same entry as today.
7. A 500-turn synthetic session scrolls at interactive frame rates.
