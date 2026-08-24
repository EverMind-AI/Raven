# TUI DAG dependency picture - design

Date: 2026-08-23
Status: implemented

## Goal

Draw the dependency structure of a `run_subagent_dag` call as an actual graph
in the TUI transcript: boxed nodes laid out left to right by depth, connected
by routed box-drawing wires, sitting directly above the per-node detail rows
the panel already shows.

## Why this is needed

`DagPanel` today carries the topology two ways, and neither one lets a reader
see the dependency structure.

Levels. `layoutDag` (`ui-tui/src/lib/dagLayout.ts`) assigns each node to a
level one below its deepest dependency, and the panel prints the level number
in a four-cell gutter (`L1`, `L2`, ...). That says how deep a node is. It does
not say what it waits on.

Named dependencies. `dagNodeNames` (`ui-tui/src/lib/dagStatus.ts:80`) appends
` <- dep1, dep2` to the row, naming dependencies by node id. But a row does not
show its own id - by deliberate design, since the id is generated and is the
widest thing on the row, so the row shows `subagent: <query summary>` instead
and reveals the id only in the block a click opens. So the trailing dependency
list names rows the reader cannot identify. Resolving one edge takes a click
per candidate row.

Both modules say so in their own header comments: "a terminal cannot route
edges the way a canvas can". That premise is what this design replaces. The web
UI already draws the real thing (`ui-webui/frontend/src/components/dag/DagGraph.tsx`,
React Flow, left to right by longest-path depth); the terminal has been the
surface where a multi-node run is hardest to read, which is backwards - the TUI
is where these runs are usually launched.

## Scope

In:

- A picture block between the run header and the detail rows, in `DagPanel`,
  so it appears in both places the panel renders: the live panel during a turn
  (`streamingAssistant.tsx`) and under the tool call row in the transcript
  (`episodeView.tsx:270`, `dagFor`).
- Short ordinals (`1..N`) shared by the picture and the detail rows.
- Detail-row changes that follow from the picture existing.
- `/dag <ordinal>` as an alias for `/dag <node-id>`.

Out:

- Any new view, overlay, or key binding. The picture is inline only.
- Edges for sequencing induced by a shared `instance` handle. Nodes naming the
  same instance run sequentially, but that is a scheduling constraint, not a
  data dependency; the web UI does not draw it either, and the detail row
  already shows `@instance`.
- Node durations. The web UI shows them; the TUI panel never has, and adding
  them is a separate question from drawing the graph.

## Decisions

1. **Boxed nodes, three rows each.** Rejected alternatives, in increasing
   density: a single-row chip (`[1 ✓ research]`), a bare ordinal plus status
   glyph (`1✓`), and a `git log --graph` style lane rail down the left of the
   existing rows. The boxes cost height; they were chosen anyway because the
   picture is meant to be looked at, not decoded.
2. **No height cap.** A six-way parallel fan is 18 picture rows and that is
   accepted. Only width drives degradation.
3. **Width degradation is automatic, in two steps.** Full labels
   (`1 ✓ research`) -> compact labels (`1 ✓`) -> no picture at all. Measured
   against the width the panel is handed, with no user-facing setting.
4. **The picture never reflows.** Geometry is a function of the node set and
   its edges only. Confirmed rather than assumed: all seven status glyphs
   (`○ ● ✓ ✗ ⊘ ⊗ ■`), the arrowhead `▸`, and all eleven wire
   characters (`─ │ ╭ ╮ ╰ ╯ ├ ┤ ┬ ┴ ┼`) measure exactly one cell
   under the fork's `stringWidth`, so a status transition can never change a
   box's width.
5. **Trailing `(subagent)` disappears when there is no instance.** The row
   already opens with `subagent: `, so the parenthetical repeated it before the
   picture existed; with the box naming the agent a third time it is noise.
   `(subagent@instance)` stays, because the ordinal is information.
6. **In-graph dependencies leave the row text.** The picture draws them. A
   dependency the picture cannot draw - one naming a node from an earlier run
   of the session, which `layoutDag` treats as satisfied and which has no box -
   stays as ` <- name` text. When the picture is elided for width, every
   dependency comes back as text.

## Architecture

Two new pure modules, split so that the geometry can be asserted as numbers and
the drawing as strings. A wrong lane index and a wrong corner character are
different bugs and should fail different tests.

### `lib/dagGraphLayout.ts`

`layoutDagGraph(levels: DagLevel[], opts: { width: number }) -> DagGraphGeometry | null`

Consumes `layoutDag`'s output unchanged: a level index is exactly the
longest-path depth, which is the column number. `dagLayout.ts` therefore needs
no code change - only its header comment, which currently states the premise
this design drops.

**Columns.** Column `c` holds the nodes at level `c`.

**Bands.** Within a column, nodes stack into bands, one band per node. A band
is a global row index: band `i` occupies picture rows `3i .. 3i+2` in every
column, so boxes line up across columns. Band order within a column comes from
one barycentre pass, left to right - a node sits at the mean band of its
dependencies. Ties keep submitted order, and column 0 keeps submitted order
outright, so the ordering is a pure function of the graph.

**Flyover rows.** An edge spanning more than one column cannot travel through
an intermediate column's box area. Rather than spend a three-row band on a
Sugiyama dummy node, each such edge gets a one-row channel below all bands.
Flyover rows are packed: two long edges share a row when their column spans do
not overlap. Total height is `3 * bands + flyoverRows`.

**Gutter lanes.** Between two columns sits a gutter carrying the vertical part
of every edge that turns there. Each edge contributes a stub: a short edge one
stub in gutter `col(src)`; a long edge two, one in `col(src)` (source row down
to its flyover row) and one in `col(tgt)-1` (flyover row up to target row).

Stubs merge into a shared vertical trunk under exactly one rule: they share an
endpoint. Grouped by source when that source forks in this gutter, else by
target when that target joins in it, else left alone. This rule is the
correctness core of the layout - merging any two stubs that do not share an
endpoint draws a connection the graph does not contain. Grouping by connected
component would do exactly that: with `A->C`, `B->C`, `B->D`, the component
spans all three edges and a shared trunk would assert `A->D`.

Groups whose stops all sit on one row need no vertical and take no lane. The
rest are packed into lanes by row-interval overlap - and that packing must treat
a touch as a conflict, not just a true overlap. Two trunks in one column, one
ending on the row the other begins, put two turns in a single cell, and the
junction character there then claims a connection the graph does not contain.
Flyover rows pack the other way, allowing a touch: their spans are column
indices, and two flyovers meeting at a column are still a whole gutter apart in
cells, so their lines never join. The gutter is `2 * pad + lanes + 1` cells wide
(the `+1` is the arrowhead column).

**Degradation.** Box width is `max(label width) + 4` over a uniform label
(`<ordinal> <glyph> <agent>`, with a `raven-` prefix stripped). If total width
exceeds the budget, the label drops to `<ordinal> <glyph>` and the geometry is
recomputed. If it still does not fit, the function returns `null` and the panel
draws no picture.

**Output.** `DagGraphGeometry` holds box rectangles keyed by node id, edge
paths as orthogonal point lists, the chosen label style, and total extent.

### `lib/dagGraphRender.ts`

`renderDagGraph(geometry: DagGraphGeometry) -> DagPictureSpan[][]`

Rasterises onto a character grid in two passes.

Wires first: every edge path's segments OR a four-bit direction mask
(up/down/left/right) into each cell they cross. A single lookup then turns each
mask into its character. Crossings, forks and joins need no special case -
`up|down|left|right` is a crossing, `up|left|right` is a join from below, and
both fall out of the same table. Corners use the rounded forms so the wires
match the box borders.

Boxes second, overwriting wire cells, so a box always wins its own area. An
arrowhead is placed in the cell immediately left of a box that has at least one
incoming edge, and only there - a flyover passing under a column gets no
arrowhead, and a root node gets none either, so the arrow always means "an edge
arrives here".

Rows are emitted as spans rather than strings so the component can colour and
click them:

```ts
type DagPictureSpanKind = 'wire' | 'border' | 'label'
interface DagPictureSpan { text: string; kind: DagPictureSpanKind; nodeId?: string }
```

### Colour

- `wire`: `t.color.border`, dim.
- `border`: `t.color.border`; `t.color.error` when the node's status is
  `failed` or `cancelled`. Only those two tint the frame - a failure has to be
  findable at a glance, while five differently-coloured frames would compete
  with each other and with the transcript around them.
- `label`: the status glyph takes `DAG_STATUS_GLYPH[status].color(t)`, the rest
  takes `t.color.text`, or `t.color.muted` for `pending` and `skipped`. Same
  rule the detail row already applies.

### Component

`DagPanel` renders header, picture, detail rows. The `L{n}` gutter and the
per-level grouping go; rows become one flat list in ordinal order, each prefixed
with its ordinal. A box's spans are wrapped in a `Box` carrying `onClick` - only
`Box` carries mouse props in this fork - dispatching the same
`toggleDagNode(dagNodeKey(runId, nodeId))` the row does, so clicking a box and
clicking its row open the same prompt block.

### Ordinals

`1..N` over `run.nodes` in array order, which is the order `dag.run_started`
delivered and which `foldDagEvent` never reorders. Stable for the run's life,
including across a `dag.get` snapshot repair, which rebuilds the node list from
the manifest in the same order.

### `/dag`

The argument is matched as a node id first, exactly as before. Only when that
misses is a bare number read as a ordinal, and only against the turn's most
recent run - the graph whose ordinals the user is reading. Trying the id first is
what keeps the promise that existing usage is unaffected: a node whose generated
id happens to be numeric still resolves to itself rather than to whatever box
sits at that position. A number matching neither reports no such node rather
than searching earlier runs for it. The no-argument refresh listing changes from `nodes: a, b, c` to
`nodes: 1 a, 2 b, 3 c` so the mapping is printed where the ids are.

## Worked examples

All of these are output from the reference implementation in the appendix, not
hand-drawn.

Five-node diamond, complete, with the detail rows and the run footer:

```
dag-3f2a1b  5 nodes · 5 done

  ╭──────────────╮      ╭──────────────╮      ╭──────────────╮
  │ 1 ✓ research │──┬──▸│   3 ✓ code   │──┬──▸│   5 ✓ code   │
  ╰──────────────╯  │   ╰──────────────╯  │   ╰──────────────╯
  ╭──────────────╮  │   ╭──────────────╮  │
  │ 2 ✓ research │──┴──▸│   4 ✓ code   │──╯
  ╰──────────────╯      ╰──────────────╯

  1 ✓ raven-research: map the prior art on tool-call tracing
  2 ✓ raven-research: read RFC 9110 end to end, note the caching rules
  3 ✓ raven-code: run the benchmark suite against both backends
  4 ✓ raven-code: merge findings into one note
  5 ✓ raven-code: write the summary for the team

  outputs in .ravenx_dag/dag-3f2a1b
```

Fan-out into fan-in - four sources joining one target share a single trunk,
not four parallel verticals. A long edge spanning two columns dips to a flyover
row below the boxes and comes back up, crossing intermediate wires as `+`
without touching any box. Both are reproduced in the appendix.

Measured extents:

| Graph | Rows | Cols (full / compact) |
|---|---|---|
| 5-node diamond | 6 | 60 / 33 |
| 6-deep chain | 3 | 102 / 72 |
| fan-out + fan-in, 6 nodes | 12 | 60 / 33 |
| 8 nodes with a long edge | 13 | 82 / 46 |
| 6-way parallel | 18 | 60 / 33 |

## Files

| File | Change |
|---|---|
| `ui-tui/src/lib/dagGraphLayout.ts` | new |
| `ui-tui/src/lib/dagGraphRender.ts` | new |
| `ui-tui/src/lib/dagLayout.ts` | header comment only |
| `ui-tui/src/lib/dagStatus.ts` | `dagNodeNames(node, drawn)` |
| `ui-tui/src/components/dagPanel.tsx` | picture block, flat rows, ordinals, box clicks, header comment |
| `ui-tui/src/app/slash/commands/dag.ts` | ordinal argument, numbered refresh listing |

`layoutDag` and `dagNodeNames` are each imported by `dagPanel.tsx` and nothing
else, so the blast radius is the panel plus its tests.

## Testing

New, under `ui-tui/src/__tests__/`:

- `dagGraphLayout.test.ts` - diamond, fan-out, fan-in, a long edge (asserting
  the flyover row index and its column span), a single node, a node with no
  dependencies, a cycle (`layoutDag` emits the unresolvable remainder as one
  final level; routing must terminate on it), a dependency naming a node
  outside the run, and the two degradation thresholds.
- `dagGraphRender.test.ts` - each shape compared line by line as a string, plus
  two invariants: rendering the same input twice is byte-identical, and
  changing only statuses leaves the geometry unchanged. The second one is what
  pins down the promise that the picture does not jump while a run advances.

Extended, not replaced: `dagPanel.test.tsx`, `dagStatus.test.ts`,
`dagCommand.test.ts`.

Verification: `npx vitest run src/__tests__/dag`, `npm run type-check`,
`npm run lint`, all from `ui-tui/`. A full `npm run test` needs a serial run;
these ink render tests are flaky under the default worker parallelism. Checking
the result in a real terminal needs `npm run build --prefix ui-tui` first,
because `raven tui` loads the prebuilt `ui-tui/dist/entry.js`.

## What makes a node expandable

A row and its box are clickable only when the node carries a `promptTemplate`.
Nothing else can be revealed: no `dag.*` frame carries a node's prompt, and the
tool result kept in the transcript is clamped to 200 chars.

That template reaches the client on `tool.start`, keyed by tool call id, and is
attached when `dag.run_started` is folded. The two frames travel on **different
channels** -- `dag.*` on the DAG tool's own progress sink, `tool.start` through
the delivery hub -- so either can arrive first. Measured on a real run
(2026-08-23) the server emitted them 2 ms apart. Attaching only at
`dag.run_started` therefore let that margin decide whether any row of the graph
could be expanded, and nothing put the prompts back afterwards: the rows fell
back to printing their node ids and silently carried no click handler.

`withPromptTemplates` (`domain/dagRun.ts`) is the other half, applied by
`recordDagPrompts` when a run for that call is already open. It re-pins the
episode row, because that row holds the run state by reference and the backfill
replaces the object. This predates the graph work -- the same gate disables the
click on `main` -- but the graph made it conspicuous, since a template-less run
shows agent names in its boxes and bare ids in its rows.

## Risks

- **Transcript height.** A wide graph is expensive and the panel is rendered
  unconditionally (`episodeView.tsx`, four call sites, none gated on the tool
  row's open state). Accepted by decision 3. If it proves annoying, the cheapest
  retreat is a height budget that falls back to the single-row chip style,
  which needs the geometry module only.
- **Ambiguous-width terminals.** Every glyph in the picture is East Asian
  Ambiguous. A terminal configured to render ambiguous characters double-width
  shears the grid. This is not a new exposure: `appChrome`, `agentsOverlay` and
  `markdown` already mix the same characters with ASCII, so the TUI already
  assumes ambiguous-narrow.
- **Junction glyphs are ambiguous where two edges turn on one row.** Distinct
  trunks never share a column, but a horizontal leaving a box still crosses any
  trunk between it and its own, and where that trunk *ends* on the same row the
  cell becomes a `T` join rather than a crossing - which reads as "these two
  meet" when they only pass. It needs a node's outgoing row to coincide with
  another edge's turn row in the same gutter, so it is uncommon, and no
  information is lost: the boxes and the rows still say what depends on what.
  Ordering lanes by which of them a crossing horizontal has to reach would
  remove most cases and is the obvious follow-up if it proves distracting.
- **Rollback** is deleting the picture block from `DagPanel` and restoring the
  two `dagNodeNames` behaviours. The two new modules are unreferenced after
  that and can be removed separately.

## Appendix: reference implementation

A Python prototype that produced every picture in this document. It is the
executable statement of the layout and routing rules; the TypeScript modules
are a port of it, not a reinterpretation.

```python
"""Prototype: the horizontal box-style DAG picture for the Raven TUI.

Columns are longest-path depth, bands are parallel slots, and every edge is
rasterised onto a char grid as orthogonal segments carrying 4-bit direction
masks -- so crossings, forks and joins compose without special cases."""

from dataclasses import dataclass

U, D, L, R = 1, 2, 4, 8

MASK = {U | D: "│", L | R: "─", D | R: "╭", D | L: "╮",
        U | R: "╰", U | L: "╯", U | D | R: "├", U | D | L: "┤",
        D | L | R: "┬", U | L | R: "┴", U | D | L | R: "┼",
        U: "│", D: "│", L: "─", R: "─"}

GLYPH = {"pending": "○", "running": "●", "completed": "✓",
         "failed": "✗", "skipped": "⊘", "cancelled": "⊗",
         "interrupted": "■"}

BAND_H, PAD = 3, 2       # rows per node band; blank cells each side of a gutter


@dataclass
class Node:
    id: str
    agent: str
    deps: list
    status: str = "completed"
    summary: str = ""
    ordinal: int = 0


def render(nodes, compact=False):
    by_id = {n.id: n for n in nodes}
    for i, n in enumerate(nodes):
        n.ordinal = i + 1

    # -- columns: longest-path depth ---------------------------------
    memo = {}

    def depth(nid):
        if nid not in memo:
            memo[nid] = 0
            memo[nid] = 1 + max((depth(d) for d in by_id[nid].deps if d in by_id),
                                default=-1)
        return memo[nid]

    col = {n.id: depth(n.id) for n in nodes}
    ncol = max(col.values()) + 1
    cols = [[n.id for n in nodes if col[n.id] == c] for c in range(ncol)]

    # One barycentre pass so parallel branches line up with their parents.
    band = {}
    for c, ids in enumerate(cols):
        if c:
            ids.sort(key=lambda i: (sum(band[d] for d in by_id[i].deps if d in band)
                                    / max(1, len([d for d in by_id[i].deps if d in band]))
                                    if any(d in band for d in by_id[i].deps) else 0))
        for r, i in enumerate(ids):
            band[i] = r
    B = max(len(ids) for ids in cols)

    edges = [(d, n.id) for n in nodes for d in n.deps if d in by_id]
    longs = [e for e in edges if col[e[1]] - col[e[0]] > 1]

    # -- flyover rows: one packed band per long edge, below the boxes -
    fly, taken = {}, []
    for s, t in longs:
        lo, hi = col[s], col[t]
        for j, spans in enumerate(taken):
            if all(hi <= a or lo >= b for a, b in spans):
                spans.append((lo, hi)); fly[(s, t)] = j; break
        else:
            taken.append([(lo, hi)]); fly[(s, t)] = len(taken) - 1

    rowof = lambda i: band[i] * BAND_H + 1
    flyrow = lambda j: B * BAND_H + j
    H = B * BAND_H + len(taken)

    # -- gutter lanes -------------------------------------------------
    # A stub is one edge's vertical in one gutter. Stubs merge into a shared
    # trunk only when they genuinely share an endpoint -- by source when that
    # source forks, else by target when that target joins. Merging any wider
    # would draw a connection that is not in the graph.
    stubs = [[] for _ in range(max(1, ncol - 1))]
    for s, t in edges:
        if col[t] - col[s] == 1:
            stubs[col[s]].append(((s, t), rowof(s), rowof(t), s, t))
        else:
            f = flyrow(fly[(s, t)])
            stubs[col[s]].append(((s, t), rowof(s), f, s, None))
            stubs[col[t] - 1].append(((s, t), f, rowof(t), None, t))

    lane_of, gw = [], []
    for g, items in enumerate(stubs):
        forks, joins = {}, {}
        for _, _, _, s, t in items:
            forks[s] = forks.get(s, 0) + 1
            joins[t] = joins.get(t, 0) + 1
        groups = {}
        for key, a, b, s, t in items:
            k = ("s", s) if s and forks[s] > 1 else ("t", t) if t and joins[t] > 1 else ("e", key, s, t)
            groups.setdefault(k, []).append((a, b))
        lanes, assign = [], {}
        for k, pairs in groups.items():
            lo = min(min(p) for p in pairs)
            hi = max(max(p) for p in pairs)
            if lo == hi:
                assign[k] = None
                continue
            for li, spans in enumerate(lanes):
                if all(hi < a or lo > b for a, b in spans):
                    spans.append((lo, hi)); assign[k] = li; break
            else:
                lanes.append([(lo, hi)]); assign[k] = len(lanes) - 1
        lane_of.append((groups, assign))
        gw.append(PAD * 2 + max(1, len(lanes)) + 1)

    lab_of = (lambda n: f"{n.ordinal} {GLYPH[n.status]}") if compact else (
        lambda n: f"{n.ordinal} {GLYPH[n.status]} {n.agent.removeprefix('raven-')}")
    bw = max(len(lab_of(n)) for n in nodes) + 4
    xof, x = [], 0
    for c in range(ncol):
        xof.append(x)
        x += bw + (gw[c] if c < ncol - 1 else 0)
    W = x

    grid = [[" "] * W for _ in range(H)]
    mask = [[0] * W for _ in range(H)]
    put = lambda r, c, ch: grid[r].__setitem__(c, ch)

    def hseg(r, x0, x1):
        for xx in range(min(x0, x1), max(x0, x1) + 1):
            mask[r][xx] |= (L if xx > min(x0, x1) else 0) | (R if xx < max(x0, x1) else 0)

    def vseg(c, y0, y1):
        for yy in range(min(y0, y1), max(y0, y1) + 1):
            mask[yy][c] |= (U if yy > min(y0, y1) else 0) | (D if yy < max(y0, y1) else 0)

    def lanex(g, key, s, t):
        groups, assign = lane_of[g]
        forks, joins = {}, {}
        for _, _, _, ss, tt in stubs[g]:
            forks[ss] = forks.get(ss, 0) + 1
            joins[tt] = joins.get(tt, 0) + 1
        k = ("s", s) if s and forks[s] > 1 else ("t", t) if t and joins[t] > 1 else ("e", key, s, t)
        li = assign[k]
        return None if li is None else xof[g] + bw + PAD + li

    for n in nodes:                                    # boxes
        top, x0 = band[n.id] * BAND_H, xof[col[n.id]]
        lab = lab_of(n).center(bw - 2)
        for i in range(bw):
            put(top, x0 + i, "─"); put(top + 2, x0 + i, "─")
        put(top, x0, "╭"); put(top, x0 + bw - 1, "╮")
        put(top + 2, x0, "╰"); put(top + 2, x0 + bw - 1, "╯")
        put(top + 1, x0, "│"); put(top + 1, x0 + bw - 1, "│")
        for i, ch in enumerate(lab):
            put(top + 1, x0 + 1 + i, ch)

    for s, t in edges:                                  # wires
        gs, gt = col[s], col[t] - 1
        if gs == gt:
            lx = lanex(gs, (s, t), s, t)
            a, b = rowof(s), rowof(t)
            if lx is None:
                hseg(a, xof[gs] + bw, xof[gs + 1] - 2)
            else:
                hseg(a, xof[gs] + bw, lx); vseg(lx, a, b); hseg(b, lx, xof[gs + 1] - 2)
        else:
            f = flyrow(fly[(s, t)])
            lx1 = lanex(gs, (s, t), s, None)
            lx2 = lanex(gt, (s, t), None, t)
            hseg(rowof(s), xof[gs] + bw, lx1); vseg(lx1, rowof(s), f)
            hseg(f, lx1, lx2)
            vseg(lx2, f, rowof(t)); hseg(rowof(t), lx2, xof[gt + 1] - 2)
        put(rowof(t), xof[col[t]] - 1, "▸")

    for r in range(H):
        for c in range(W):
            if mask[r][c] and grid[r][c] == " ":
                grid[r][c] = MASK[mask[r][c]]
    return [("".join(r)).rstrip() for r in grid]
```

Its output for the four shapes the design rests on:

```
5-node diamond, mid-run                                 [6 rows x 60 cols]

╭──────────────╮      ╭──────────────╮      ╭──────────────╮
│ 1 ✓ research │──┬──▸│   3 ● code   │──┬──▸│   5 ○ code   │
╰──────────────╯  │   ╰──────────────╯  │   ╰──────────────╯
╭──────────────╮  │   ╭──────────────╮  │
│ 2 ✓ research │──┴──▸│   4 ● code   │──╯
╰──────────────╯      ╰──────────────╯

fan-out into fan-in                                    [12 rows x 60 cols]

╭──────────────╮      ╭──────────────╮      ╭──────────────╮
│ 1 ✓ research │──┬──▸│   2 ✓ code   │──┬──▸│   6 ✓ code   │
╰──────────────╯  │   ╰──────────────╯  │   ╰──────────────╯
                  │   ╭──────────────╮  │
                  ├──▸│   3 ✓ code   │──┤
                  │   ╰──────────────╯  │
                  │   ╭──────────────╮  │
                  ├──▸│   4 ✓ code   │──┤
                  │   ╰──────────────╯  │
                  │   ╭──────────────╮  │
                  ╰──▸│   5 ✓ code   │──╯
                      ╰──────────────╯

long edge on a flyover row (1 -> 7 spans two columns)  [13 rows x 82 cols]

╭──────────────╮      ╭──────────────╮      ╭──────────────╮      ╭──────────────╮
│ 1 ✓ research │──┬──▸│   2 ✓ code   │──┬──▸│   6 ✓ code   │──┬──▸│   7 ✓ code   │
╰──────────────╯  │   ╰──────────────╯  │   ╰──────────────╯  │   ╰──────────────╯
                  │   ╭──────────────╮  │                     │
                  ├──▸│   3 ✓ code   │──┤                     │
                  │   ╰──────────────╯  │                     │
                  │   ╭──────────────╮  │                     │
                  ├──▸│   4 ✓ code   │──┤                     │
                  │   ╰──────────────╯  │                     │
                  │   ╭──────────────╮  │                     │
                  ├──▸│   5 ✓ code   │──╯                     │
                  │   ╰──────────────╯                        │
                  ╰───────────────────────────────────────────╯

6-deep chain, compact labels (width fallback)           [3 rows x 72 cols]

╭─────╮      ╭─────╮      ╭─────╮      ╭─────╮      ╭─────╮      ╭─────╮
│ 1 ✓ │─────▸│ 2 ✓ │─────▸│ 3 ✓ │─────▸│ 4 ✓ │─────▸│ 5 ✓ │─────▸│ 6 ✓ │
╰─────╯      ╰─────╯      ╰─────╯      ╰─────╯      ╰─────╯      ╰─────╯
```
