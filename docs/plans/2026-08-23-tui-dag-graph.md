# TUI DAG dependency picture - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task by task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Draw a `run_subagent_dag` run's dependency structure as a real graph - boxed nodes
laid out left to right by depth, joined by routed box-drawing wires - above the per-node detail
rows `DagPanel` already renders.

**Architecture:** Two new pure modules. `dagGraphLayout.ts` turns the run's nodes into a
character-cell geometry (box rectangles, orthogonal edge segments, arrowheads) by reusing
`layoutDag` for column assignment. `dagGraphRender.ts` rasterises that geometry onto a char
grid using 4-bit direction masks and emits styled spans. `DagPanel` renders the spans above
its rows; nothing else in the TUI changes shape.

**Tech Stack:** TypeScript, React + `@hermes/ink`, vitest, `ink-testing-library`.

**Spec:** `docs/specs/2026-08-23-tui-dag-graph-design.md`. Its appendix holds a verified
Python reference implementation that produced every picture in this plan; the TypeScript is a
port of it, not a reinterpretation. Read the spec first.

## Global Constraints

- Branch is `feat/tui_dag_dependency_graph`, cut from `origin/main`. Do not commit on `main`.
- Every new file needs an SPDX header matching its neighbours:
  `// SPDX-License-Identifier: MIT`, `// Copyright (c) 2026 EverMind.`, `// See NOTICES.md.`
- Comments in English only, and only where the logic is non-obvious or a constraint is hidden
  (AGENTS.md 1.1, 1.2). Every new file needs a module docstring stating its purpose.
- Commits are Conventional Commits, all-English, ASCII-only, header <= 100 chars, with a
  `Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>` trailer (AGENTS.md 3).
  **Do not commit unless the user asks** (AGENTS.md 3.4) - the staging steps below stop there.
- Tests go in the existing `ui-tui/src/__tests__/dag*.test.ts(x)` files where one already
  covers the module; new modules get new files following the same naming (AGENTS.md 5).
- Run tests as `npx vitest run <path>` from `ui-tui/`. A full `npm run test` needs a serial
  run - these ink render tests are flaky under the default worker parallelism.
- Do not add dependencies. Everything needed is already present.

## File structure

| File | Responsibility |
|---|---|
| `ui-tui/src/lib/dagGraphLayout.ts` (new) | Nodes -> `DagGraphGeometry`. Columns, bands, flyover rows, gutter lanes, width degradation. Knows nothing about characters. |
| `ui-tui/src/lib/dagGraphRender.ts` (new) | `DagGraphGeometry` -> `DagPictureSpan[][]`. Mask rasterisation, box drawing, span extraction. Knows nothing about the graph. |
| `ui-tui/src/lib/dagLayout.ts` | Unchanged code. Header comment only - it currently states the premise this work removes. |
| `ui-tui/src/lib/dagStatus.ts` | `dagNodeNames` gains a `drawn` argument and drops the parenthetical when there is no instance. |
| `ui-tui/src/lib/dagOpenNodes.ts` | Gains `dagSpanToggleKey` - what a picture span opens, so the box and its row cannot disagree. |
| `ui-tui/src/components/dagPanel.tsx` | Renders the picture, then a flat ordinal-prefixed row list. Box spans are clickable. |
| `ui-tui/src/app/slash/commands/dag.ts` | Numeric argument resolves to a ordinal; refresh listing numbers the ids. |

The layout/render seam is deliberate: a wrong lane index and a wrong corner character are
different bugs, and asserting numbers is a different exercise from asserting strings.

---

### Task 1: Geometry for graphs whose edges span one column

**Files:**
- Create: `ui-tui/src/lib/dagGraphLayout.ts`
- Test: `ui-tui/src/__tests__/dagGraphLayout.test.ts`

**Interfaces:**
- Consumes: `layoutDag` from `../lib/dagLayout.js`, `DAG_STATUS_GLYPH` from
  `../lib/dagStatus.js`, `DagRunNode` from `../domain/dagRun.js`.
- Produces:

```ts
export const BOX_BAND_HEIGHT = 3
export const GUTTER_PAD = 2

export interface DagGraphBox {
  nodeId: string
  /** Cell column of the box's left border. */
  x: number
  /** Cell row of the box's top border; the label sits at `y + 1`. */
  y: number
  width: number
  /** Label already centred to `width - 2` cells. */
  label: string
}

/** One orthogonal run. Horizontal when `y0 === y1`, vertical when `x0 === x1`. */
export interface DagGraphSegment {
  x0: number
  y0: number
  x1: number
  y1: number
}

export interface DagGraphGeometry {
  boxes: DagGraphBox[]
  segments: DagGraphSegment[]
  /** Cells taking an arrowhead: one left of every box with an incoming edge. */
  arrows: { x: number; y: number }[]
  /** `${dep}>${node}` for every edge the picture draws. */
  drawn: ReadonlySet<string>
  width: number
  height: number
  style: 'compact' | 'full'
}

export const layoutDagGraph = (
  nodes: readonly DagRunNode[],
  opts: { width: number }
): DagGraphGeometry | null
```

- [ ] **Step 1: Write the failing tests**

Create `ui-tui/src/__tests__/dagGraphLayout.test.ts`:

```ts
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { DagRunNode, DagRunNodeStatus } from '../domain/dagRun.js'

import { BOX_BAND_HEIGHT, layoutDagGraph } from '../lib/dagGraphLayout.js'

const node = (id: string, dependsOn: string[] = [], status: DagRunNodeStatus = 'completed'): DagRunNode => ({
  id,
  subagent: 'raven-code',
  dependsOn,
  status
})

const WIDE = { width: 200 }

const laid = (nodes: DagRunNode[], width = 200) => layoutDagGraph(nodes, { width })!

const boxOf = (geometry: DagGraphGeometry, id: string) => geometry.boxes.find(box => box.nodeId === id)!

const trunks = (geometry: DagGraphGeometry) =>
  new Set(geometry.segments.filter(s => s.x0 === s.x1).map(s => s.x0))

describe('layoutDagGraph', () => {
  it('puts each step of a chain in its own column, all on one band', () => {
    const g = laid([node('a'), node('b', ['a']), node('c', ['b'])])

    expect(g.boxes.map(box => box.y)).toEqual([0, 0, 0])
    expect(boxOf(g, 'a').x).toBeLessThan(boxOf(g, 'b').x)
    expect(boxOf(g, 'b').x).toBeLessThan(boxOf(g, 'c').x)
    expect(g.height).toBe(BOX_BAND_HEIGHT)
  })

  it('stacks independent nodes into bands of one column', () => {
    const g = laid([node('a'), node('b'), node('c')])

    expect(g.boxes.map(box => box.x)).toEqual([0, 0, 0])
    expect(g.boxes.map(box => box.y)).toEqual([0, BOX_BAND_HEIGHT, BOX_BAND_HEIGHT * 2])
  })

  it('gives every box the same width, so the columns line up', () => {
    const g = laid([node('a'), node('bbbbbbbbbb', ['a'])])

    expect(new Set(g.boxes.map(box => box.width)).size).toBe(1)
  })

  it('draws an edge for every in-graph dependency', () => {
    const g = laid([node('a'), node('b'), node('c', ['a', 'b'])])

    expect([...g.drawn].sort()).toEqual(['a>c', 'b>c'])
  })

  it('leaves a dependency outside the run undrawn, so the row can still name it', () => {
    // `depends_on` may name a node an earlier run of the session completed. It
    // has no box, so it has no edge -- and the row has to say so in words.
    const g = laid([node('a', ['ghost'])])

    expect(g.drawn.size).toBe(0)
    expect(g.segments).toEqual([])
  })

  it('puts an arrowhead left of a node that has an incoming edge, and nowhere else', () => {
    const g = laid([node('a'), node('b', ['a'])])
    const b = boxOf(g, 'b')

    expect(g.arrows).toEqual([{ x: b.x - 1, y: b.y + 1 }])
  })

  it('routes a fan-out through one shared trunk', () => {
    // Two edges leaving the same node share a vertical: they genuinely share an
    // endpoint. Two unrelated edges must never share one, or the picture asserts
    // a connection the graph does not contain.
    expect(trunks(laid([node('a'), node('b', ['a']), node('c', ['a'])])).size).toBe(1)
  })

  it('routes a fan-in through one shared trunk', () => {
    expect(trunks(laid([node('a'), node('b'), node('c', ['a', 'b'])])).size).toBe(1)
  })

  it('keeps two unrelated crossing edges on separate trunks', () => {
    // a -> d and b -> c cross. One trunk would draw a -> c and b -> d as well.
    expect(trunks(laid([node('a'), node('b'), node('c', ['b']), node('d', ['a'])])).size).toBe(2)
  })

  it('spends no trunk on an edge whose ends share a band', () => {
    expect(trunks(laid([node('a'), node('b', ['a'])])).size).toBe(0)
  })

  it('orders a band by the mean band of its dependencies', () => {
    // Without the barycentre pass `y` follows submitted order and the wires
    // cross for no reason.
    const g = laid([node('a'), node('b'), node('x', ['b']), node('y', ['a'])])

    expect(boxOf(g, 'y').y).toBeLessThan(boxOf(g, 'x').y)
  })

  it('keeps the submitted order of the first column', () => {
    // The picture is rebuilt on every progress event; ordering by anything that
    // changes as the run advances would make the boxes jump.
    const g = laid([node('z'), node('m'), node('a')])

    expect(g.boxes.map(box => box.nodeId)).toEqual(['z', 'm', 'a'])
  })

  it('still places every node of a cyclic graph', () => {
    // The backend rejects cycles before running, so only a malformed frame gets
    // here -- but looping forever while drawing is worse than an imperfect draw.
    const g = laid([node('a', ['b']), node('b', ['a'])])

    expect(g.boxes.map(box => box.nodeId).sort()).toEqual(['a', 'b'])
  })

  it('drops an edge that does not point rightwards', () => {
    // Only reachable through the cycle fallback above. Such an edge has no
    // left-to-right route, so it is left to the row text rather than drawn wrong.
    expect(laid([node('a', ['b']), node('b', ['a'])]).drawn.size).toBeLessThanOrEqual(1)
  })

  it('returns null for an empty graph', () => {
    expect(layoutDagGraph([], WIDE)).toBeNull()
  })
})
```

Add `import type { DagGraphGeometry } from '../lib/dagGraphLayout.js'` alongside the value
import.

- [ ] **Step 2: Run the tests to verify they fail**

Run from `ui-tui/`: `npx vitest run src/__tests__/dagGraphLayout.test.ts`
Expected: FAIL - cannot resolve `../lib/dagGraphLayout.js`.

- [ ] **Step 3: Implement the geometry**

Create `ui-tui/src/lib/dagGraphLayout.ts`. Port the reference implementation from the spec
appendix, minus the flyover and degradation parts (Tasks 2 and 3 add those). In order:

1. `layoutDag([...nodes])` gives the levels; a node's level index is its column. Reuse it
   rather than recomputing depth - it is already tested, and its cycle fallback (emit the
   unresolvable remainder as one final level) is already specified there.
2. Column order starts as the level's own order, then one barycentre pass for columns after
   the first: sort by the mean band of the node's already-placed dependencies, falling back to
   the node's current index. `Array.prototype.sort` is stable, so ties keep submitted order.
3. Band `i` occupies rows `i * BOX_BAND_HEIGHT .. i * BOX_BAND_HEIGHT + 2`; a wire attaches at
   the middle row `i * BOX_BAND_HEIGHT + 1`. Bands are global, which is what makes boxes line
   up across columns.
4. Box label is `` `${ordinal} ${glyph} ${agent}` `` - `ordinal` the 1-based index into `nodes`,
   `glyph` from `DAG_STATUS_GLYPH[node.status].glyph`, `agent` from
   `node.subagent.replace(/^raven-/, '')`. Box width is `max(label length) + 4`, uniform over
   the whole picture, and the stored `label` is centred to `width - 2`.
5. Edges are `[dep, node.id]` pairs where `dep` is a node of this run **and**
   `col[node.id] - col[dep] === 1` (Task 2 lifts that to `> 0`). Everything else stays out of
   `drawn`, which is what lets the row name it instead.
6. Gutter lanes: group each gutter's stubs by source when that source forks in that gutter,
   else by target when that target joins in it, else alone. A group whose rows are all equal
   needs no vertical and takes no lane. The rest pack greedily by row-interval overlap. Gutter
   width is `GUTTER_PAD * 2 + max(1, lanes) + 1`; the trailing `+1` is the arrowhead column.
7. Emit `segments` as the runs of each edge (out of the box, along the trunk, into the target),
   dropping zero-length ones, plus one `arrows` entry per box with an incoming edge.

No colour, characters or theme in this module. The only string it produces is `label`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npx vitest run src/__tests__/dagGraphLayout.test.ts`
Expected: PASS, 15 tests.

- [ ] **Step 5: Stage and report**

```bash
git add ui-tui/src/lib/dagGraphLayout.ts ui-tui/src/__tests__/dagGraphLayout.test.ts
```

Report and stop. Do not commit without the user's word (AGENTS.md 3.4).

---

### Task 2: Long edges on flyover rows

**Files:**
- Modify: `ui-tui/src/lib/dagGraphLayout.ts`
- Test: `ui-tui/src/__tests__/dagGraphLayout.test.ts`

**Interfaces:**
- Consumes: everything Task 1 produced.
- Produces: no signature change. `height` becomes
  `bands * BOX_BAND_HEIGHT + flyoverRows`, and edges spanning more than one column now appear
  in `drawn`.

- [ ] **Step 1: Write the failing tests**

Append to `ui-tui/src/__tests__/dagGraphLayout.test.ts`:

```ts
describe('layoutDagGraph long edges', () => {
  const SKIPPER = [node('a'), node('b', ['a']), node('c', ['b', 'a'])]

  const bandRows = (geometry: DagGraphGeometry) =>
    Math.max(...geometry.boxes.map(box => box.y)) + BOX_BAND_HEIGHT

  it('draws an edge that spans more than one column', () => {
    expect([...laid(SKIPPER).drawn].sort()).toEqual(['a>c', 'b>c'])
  })

  it('routes it on a row below every box rather than through a box band', () => {
    // A three-row Sugiyama dummy in the intermediate column would cost a whole
    // band; a one-row channel under the picture costs one row and reads as a
    // bypass.
    const g = laid(SKIPPER)

    expect(g.height).toBe(bandRows(g) + 1)
    expect(g.segments.some(s => s.y0 === s.y1 && s.y0 === bandRows(g))).toBe(true)
  })

  it('packs two long edges whose column spans do not overlap onto one row', () => {
    const g = laid([node('a'), node('b', ['a']), node('c', ['b', 'a']), node('d', ['c']), node('e', ['d', 'c'])])

    expect(g.height).toBe(bandRows(g) + 1)
  })

  it('gives two overlapping long edges a row each', () => {
    const g = laid([node('a'), node('b'), node('c', ['a']), node('d', ['c', 'a']), node('e', ['c', 'b'])])

    expect(g.height).toBe(bandRows(g) + 2)
  })

  it('puts no arrowhead on a flyover, only at the box it finally reaches', () => {
    const g = laid(SKIPPER)

    expect(g.arrows).toEqual([
      { x: boxOf(g, 'b').x - 1, y: boxOf(g, 'b').y + 1 },
      { x: boxOf(g, 'c').x - 1, y: boxOf(g, 'c').y + 1 }
    ])
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run src/__tests__/dagGraphLayout.test.ts -t 'long edges'`
Expected: FAIL - `a>c` missing from `drawn`, height one row short.

- [ ] **Step 3: Implement flyover rows**

In `layoutDagGraph`:

1. Split edges into short (`col diff === 1`) and long (`col diff > 1`).
2. Pack the long ones: an edge's channel spans columns `[col(src), col(tgt)]` and may share a
   flyover row with any edge whose span does not overlap. Greedy first fit, in edge order.
3. Flyover row `j` sits at `bands * BOX_BAND_HEIGHT + j`; `height` grows by the number of rows
   used.
4. A long edge contributes **two** stubs, not one: in gutter `col(src)` from the source's row
   down to its flyover row, and in gutter `col(tgt) - 1` from the flyover row up to the
   target's row. Group the first by source and the second by target, exactly as short-edge
   stubs are grouped - a long edge leaving a forking node shares that fork's trunk, which is
   correct because they do share the source.
5. Its segments are: out of the source box to the descent trunk, down to the flyover row,
   across the flyover row to the ascent trunk, up to the target's row, into the target box. The
   flyover run passes under the intermediate columns and emits no arrowhead.

- [ ] **Step 4: Run to verify they pass**

Run: `npx vitest run src/__tests__/dagGraphLayout.test.ts`
Expected: PASS, 20 tests.

- [ ] **Step 5: Stage**

```bash
git add ui-tui/src/lib/dagGraphLayout.ts ui-tui/src/__tests__/dagGraphLayout.test.ts
```

---

### Task 3: Width degradation

**Files:**
- Modify: `ui-tui/src/lib/dagGraphLayout.ts`
- Test: `ui-tui/src/__tests__/dagGraphLayout.test.ts`

**Interfaces:**
- Produces: `style` is `'full'` when labels are `` `${ordinal} ${glyph} ${agent}` `` and
  `'compact'` when they are `` `${ordinal} ${glyph}` ``. `null` when even compact overflows.

- [ ] **Step 1: Write the failing tests**

Append:

```ts
describe('layoutDagGraph width degradation', () => {
  const CHAIN = [node('a'), node('b', ['a']), node('c', ['b']), node('d', ['c'])]

  it('names the agent when there is room', () => {
    const g = laid(CHAIN)

    expect(g.style).toBe('full')
    expect(g.boxes[0]!.label).toContain('code')
  })

  it('drops the agent name rather than the picture when it does not fit', () => {
    const full = laid(CHAIN)
    const tight = laid(CHAIN, full.width - 1)

    expect(tight.style).toBe('compact')
    expect(tight.boxes[0]!.label).not.toContain('code')
    expect(tight.width).toBeLessThanOrEqual(full.width - 1)
  })

  it('keeps the ordinal and the status glyph in the compact label', () => {
    expect(laid(CHAIN, 40).boxes[0]!.label.trim()).toBe('1 ✓')
  })

  it('gives up rather than overflow the row', () => {
    expect(layoutDagGraph(CHAIN, { width: 10 })).toBeNull()
  })

  it('never reports a width above the budget', () => {
    for (const width of [30, 40, 60, 80, 120]) {
      const g = layoutDagGraph(CHAIN, { width })

      if (g) {
        expect(g.width).toBeLessThanOrEqual(width)
      }
    }
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run src/__tests__/dagGraphLayout.test.ts -t 'width degradation'`
Expected: FAIL - `style` undefined, budget not respected.

- [ ] **Step 3: Implement**

Extract the body into a private `build(nodes, style)` that returns a geometry regardless of
budget, then:

```ts
export const layoutDagGraph = (
  nodes: readonly DagRunNode[],
  opts: { width: number }
): DagGraphGeometry | null => {
  if (nodes.length === 0) {
    return null
  }

  const full = build(nodes, 'full')

  if (full.width <= opts.width) {
    return full
  }

  const compact = build(nodes, 'compact')

  return compact.width <= opts.width ? compact : null
}
```

`build` picks the label shape from `style`; everything downstream follows from the box width it
produces.

- [ ] **Step 4: Run to verify they pass**

Run: `npx vitest run src/__tests__/dagGraphLayout.test.ts`
Expected: PASS, 25 tests.

- [ ] **Step 5: Stage**

```bash
git add ui-tui/src/lib/dagGraphLayout.ts ui-tui/src/__tests__/dagGraphLayout.test.ts
```

---

### Task 4: Rasterise the geometry into styled spans

**Files:**
- Create: `ui-tui/src/lib/dagGraphRender.ts`
- Test: `ui-tui/src/__tests__/dagGraphRender.test.ts`

**Interfaces:**
- Consumes: `DagGraphGeometry` from Task 3.
- Produces:

```ts
export type DagPictureSpanKind = 'border' | 'glyph' | 'label' | 'wire'

export interface DagPictureSpan {
  text: string
  kind: DagPictureSpanKind
  /** The node a box span belongs to. Absent on wires. */
  nodeId?: string
}

export const renderDagGraph = (geometry: DagGraphGeometry): DagPictureSpan[][]
```

- [ ] **Step 1: Write the failing tests**

Create `ui-tui/src/__tests__/dagGraphRender.test.ts`:

```ts
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { DagRunNode, DagRunNodeStatus } from '../domain/dagRun.js'

import { layoutDagGraph } from '../lib/dagGraphLayout.js'
import { renderDagGraph } from '../lib/dagGraphRender.js'

const node = (
  id: string,
  dependsOn: string[] = [],
  status: DagRunNodeStatus = 'completed',
  subagent = 'raven-code'
): DagRunNode => ({ id, subagent, dependsOn, status })

const draw = (nodes: DagRunNode[], width = 200) =>
  renderDagGraph(layoutDagGraph(nodes, { width })!)
    .map(row =>
      row
        .map(span => span.text)
        .join('')
        .replace(/\s+$/, '')
    )
    .join('\n')

const DIAMOND = [
  node('survey', [], 'completed', 'raven-research'),
  node('rfc', [], 'completed', 'raven-research'),
  node('bench', ['survey']),
  node('synth', ['survey', 'rfc']),
  node('report', ['bench', 'synth'])
]

describe('renderDagGraph', () => {
  it('draws a diamond', () => {
    expect(draw(DIAMOND)).toBe(
      [
        '╭──────────────╮      ╭──────────────╮      ╭──────────────╮',
        '│ 1 ✓ research │──┬──▸│   3 ✓ code   │──┬──▸│   5 ✓ code   │',
        '╰──────────────╯  │   ╰──────────────╯  │   ╰──────────────╯',
        '╭──────────────╮  │   ╭──────────────╮  │',
        '│ 2 ✓ research │──┴──▸│   4 ✓ code   │──╯',
        '╰──────────────╯      ╰──────────────╯'
      ].join('\n')
    )
  })

  it('draws a chain with no vertical at all', () => {
    expect(draw([node('a'), node('b', ['a'])])).toBe(
      [
        '╭──────────╮      ╭──────────╮',
        '│ 1 ✓ code │─────▸│ 2 ✓ code │',
        '╰──────────╯      ╰──────────╯'
      ].join('\n')
    )
  })

  it('merges a fan-in into one trunk', () => {
    const out = draw([
      node('p', [], 'completed', 'raven-research'),
      node('f1', ['p']),
      node('f2', ['p']),
      node('f3', ['p']),
      node('m', ['f1', 'f2', 'f3'])
    ])

    expect(out).toContain('├──▸')
    expect(out).toContain('──┤')
  })

  it('runs a long edge along a row below the boxes', () => {
    const out = draw([node('a'), node('b', ['a']), node('c', ['b', 'a'])]).split('\n')

    expect(out[out.length - 1]).toMatch(/^ *╰─+╯$/)
  })

  it('marks a crossing rather than a join where two wires meet without sharing an end', () => {
    expect(draw([node('a'), node('b'), node('c', ['b']), node('d', ['a'])])).toContain('┼')
  })

  it('is byte-identical when rendered twice', () => {
    expect(draw(DIAMOND)).toBe(draw(DIAMOND))
  })

  it('keeps the geometry when only statuses change', () => {
    // This is the promise that the picture does not jump while a run advances.
    // Every status glyph is one cell wide, so a transition cannot move a box.
    const pin = (g: NonNullable<ReturnType<typeof layoutDagGraph>>) => ({
      boxes: g.boxes.map(({ nodeId, width, x, y }) => ({ nodeId, width, x, y })),
      height: g.height,
      segments: g.segments,
      width: g.width
    })
    const before = layoutDagGraph(DIAMOND, { width: 200 })!
    const after = layoutDagGraph(
      DIAMOND.map((n, i) => ({
        ...n,
        status: (i < 2 ? 'completed' : i < 4 ? 'running' : 'pending') as DagRunNodeStatus
      })),
      { width: 200 }
    )!

    expect(pin(after)).toEqual(pin(before))
  })

  it('tags each box span with its node, so the panel can colour and click it', () => {
    const spans = renderDagGraph(layoutDagGraph([node('a')], { width: 200 })!).flat()

    expect(spans.some(span => span.kind === 'border' && span.nodeId === 'a')).toBe(true)
    expect(spans.some(span => span.kind === 'glyph' && span.nodeId === 'a')).toBe(true)
    expect(spans.every(span => span.kind !== 'wire' || span.nodeId === undefined)).toBe(true)
  })

  it('emits the status glyph as its own span', () => {
    const glyphs = renderDagGraph(layoutDagGraph([node('a', [], 'failed')], { width: 200 })!)
      .flat()
      .filter(span => span.kind === 'glyph')

    expect(glyphs.map(span => span.text)).toEqual(['✗'])
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run src/__tests__/dagGraphRender.test.ts`
Expected: FAIL - cannot resolve `../lib/dagGraphRender.js`.

- [ ] **Step 3: Implement the renderer**

Create `ui-tui/src/lib/dagGraphRender.ts`, starting from:

```ts
const UP = 1
const DOWN = 2
const LEFT = 4
const RIGHT = 8

// Rounded corners so a wire's turn matches the box borders it runs between.
const MASK_CHAR: Record<number, string> = {
  [UP]: '│',
  [DOWN]: '│',
  [LEFT]: '─',
  [RIGHT]: '─',
  [UP | DOWN]: '│',
  [LEFT | RIGHT]: '─',
  [DOWN | RIGHT]: '╭',
  [DOWN | LEFT]: '╮',
  [UP | RIGHT]: '╰',
  [UP | LEFT]: '╯',
  [UP | DOWN | RIGHT]: '├',
  [UP | DOWN | LEFT]: '┤',
  [DOWN | LEFT | RIGHT]: '┬',
  [UP | LEFT | RIGHT]: '┴',
  [UP | DOWN | LEFT | RIGHT]: '┼'
}
```

Then, in order:

1. Allocate `height x width` grids: characters (spaces) and masks (zero).
2. For each segment, OR the direction bits into every cell it covers - a cell gets `RIGHT`
   when the run continues to its right and `LEFT` when it continues to its left, and likewise
   `DOWN`/`UP` for verticals. Because every edge writes into the same grid, a fork, a join and
   a crossing all fall out of the accumulated mask with no special case.
3. Draw each box over the grid - borders, then the centred label - so a box always wins its
   own cells.
4. Write `▸` at each `arrows` entry.
5. Fill any cell that still holds a space and has a non-zero mask from `MASK_CHAR`.
6. Walk each row left to right, coalescing runs of the same `(kind, nodeId)` into one span.
   Box border cells are `'border'`; the label's status glyph is `'glyph'`; the rest of the
   label is `'label'`; everything else - wires, arrowheads, padding - is `'wire'` with no
   `nodeId`.

Find the glyph by its index inside the label (it is the second space-separated field), not by
searching for the character - a search would also match a `─` drawn nearby.

- [ ] **Step 4: Run to verify they pass**

Run: `npx vitest run src/__tests__/dagGraphRender.test.ts`
Expected: PASS, 9 tests.

- [ ] **Step 5: Stage**

```bash
git add ui-tui/src/lib/dagGraphRender.ts ui-tui/src/__tests__/dagGraphRender.test.ts
```

---

### Task 5: Put the picture in the panel

**Files:**
- Modify: `ui-tui/src/components/dagPanel.tsx`
- Modify: `ui-tui/src/lib/dagStatus.ts` (`dagNodeNames`)
- Modify: `ui-tui/src/lib/dagLayout.ts` (header comment only)
- Test: `ui-tui/src/__tests__/dagPanel.test.tsx`, `ui-tui/src/__tests__/dagStatus.test.ts`

**Interfaces:**
- Consumes: `layoutDagGraph`, `renderDagGraph`.
- Produces:

```ts
export const dagNodeNames = (
  node: Pick<DagRunNode, 'dependsOn' | 'id' | 'instance' | 'subagent'>,
  drawn: ReadonlySet<string> | null
) => ({ deps: string, parens: string })
```

`deps` names only the dependencies absent from `drawn` (keyed `` `${dep}>${node.id}` ``);
`drawn === null` means no picture was rendered, so every dependency is named. `parens` is `''`
unless the node has an instance, in which case it stays `(subagent@instance)`.

- [ ] **Step 1: Write the failing tests**

In `ui-tui/src/__tests__/dagStatus.test.ts`, add `dagNodeNames` to the imports and append:

```ts
describe('dagNodeNames', () => {
  const node = { dependsOn: ['a', 'b'], id: 'c', subagent: 'raven-code' }

  it('drops the parenthetical when the node has no instance', () => {
    // The row already opens with the agent name and the picture's box carries it
    // a second time; a third copy is noise.
    expect(dagNodeNames(node, null).parens).toBe('')
  })

  it('keeps the parenthetical when there is an instance to name', () => {
    expect(dagNodeNames({ ...node, instance: 'sess1' }, null).parens).toBe('(raven-code@sess1)')
  })

  it('names every dependency when no picture was drawn', () => {
    expect(dagNodeNames(node, null).deps).toContain('a, b')
  })

  it('names only the dependencies the picture could not draw', () => {
    // An edge to a node from an earlier run has no box, so the picture cannot
    // show it and the row is the only place it survives.
    expect(dagNodeNames(node, new Set(['a>c'])).deps.trim()).toBe('← b')
  })

  it('says nothing when the picture drew every edge', () => {
    expect(dagNodeNames(node, new Set(['a>c', 'b>c'])).deps).toBe('')
  })
})
```

In `ui-tui/src/__tests__/dagPanel.test.tsx`, delete the test named
`names each node dependencies so the topology is readable` - it asserts `'parse_a, parse_b'`,
which the picture now carries instead - and add:

```tsx
  it('draws the topology as a graph above the rows', () => {
    const lines = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />).split('\n')

    expect(lines.some(line => line.includes('╭') && line.includes('╮'))).toBe(true)
    expect(lines.findIndex(line => line.includes('▸'))).toBeLessThan(
      lines.findIndex(line => line.includes('parse_a'))
    )
  })

  it('numbers the rows so a box can be matched to one', () => {
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)

    expect(f).toMatch(/1 .*fetch/)
    expect(f).toMatch(/4 .*report/)
  })

  it('stops naming a dependency the picture drew', () => {
    expect(frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)).not.toContain('parse_a, parse_b')
  })

  it('still names a dependency from an earlier run, which has no box', () => {
    const run: DagRunState = {
      runId: 'dag-x',
      done: false,
      nodes: [node('only', 'pending', ['from_an_earlier_run'])]
    }

    expect(frame(<DagPanel run={run} t={DEFAULT_THEME} />)).toContain('from_an_earlier_run')
  })

  it('drops the picture rather than overflow a narrow terminal', () => {
    const lines = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} width={24} />).split('\n')

    lines.forEach(line => expect(line.length).toBeLessThanOrEqual(24))
  })
```

Rename the existing `groups independent nodes onto the same level` to
`keeps sibling rows adjacent and their join below them` - it still passes, but its name no
longer describes what it checks.

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run src/__tests__/dagPanel.test.tsx src/__tests__/dagStatus.test.ts`
Expected: FAIL - `dagNodeNames` takes one argument; no box characters in the frame.

- [ ] **Step 3: Implement**

`dagStatus.ts`:

```ts
export const dagNodeNames = (
  node: Pick<DagRunNode, 'dependsOn' | 'id' | 'instance' | 'subagent'>,
  drawn: ReadonlySet<string> | null
) => {
  const named = drawn ? node.dependsOn.filter(dep => !drawn.has(`${dep}>${node.id}`)) : node.dependsOn

  return {
    deps: named.length > 0 ? ` ← ${named.join(', ')}` : '',
    parens: node.instance ? `(${node.subagent}@${node.instance})` : ''
  }
}
```

`dagPanel.tsx`:

1. Replace `layoutDag(run.nodes)` with `layoutDagGraph(run.nodes, { width })`, and render
   `renderDagGraph(picture)` as a `Box flexDirection="column"` of rows, each row a
   `Box flexDirection="row"` of one `Text` per span. Colour by `kind`: `wire` ->
   `t.color.border` dim; `border` -> `t.color.error` when that node's status is `failed` or
   `cancelled`, else `t.color.border`; `glyph` -> `DAG_STATUS_GLYPH[status].color(t)`;
   `label` -> `t.color.muted` for `pending`/`skipped`, else `t.color.text`.
2. Drop the level loop and the `GUTTER` constant. Rows become
   `run.nodes.map((node, index) => <NodeRow ordinal={index + 1} ... />)`.
3. `NodeRow` prints `` `${ordinal} ` `` before the status glyph and passes
   `picture?.drawn ?? null` into `dagNodeNames`. Its `room` arithmetic must account for the
   ordinal prefix and for `parens` now sometimes being empty.
4. Keep the empty-run guard: return `null` when `run.nodes.length === 0`.
5. Rewrite the header comments of both `dagPanel.tsx` and `dagLayout.ts`. Both currently open
   by saying a terminal cannot route edges and that the topology is therefore carried by
   levels plus named dependencies. That is now false in `dagPanel.tsx`, and in `dagLayout.ts`
   the level assignment has become the picture's column assignment.

- [ ] **Step 4: Run to verify they pass**

Run: `npx vitest run src/__tests__/dag`
Expected: PASS across all dag test files.

- [ ] **Step 5: Type-check and lint**

Run: `npm run type-check`, then `npm run lint`
Expected: both clean.

- [ ] **Step 6: Stage**

```bash
git add ui-tui/src/components/dagPanel.tsx ui-tui/src/lib/dagStatus.ts ui-tui/src/lib/dagLayout.ts ui-tui/src/__tests__/dagPanel.test.tsx ui-tui/src/__tests__/dagStatus.test.ts
```

---

### Task 6: Clicking a box opens its prompt

**Files:**
- Modify: `ui-tui/src/components/dagPanel.tsx`
- Test: `ui-tui/src/__tests__/dagPanel.test.tsx`

**Interfaces:**
- Consumes: `dagNodeKey`, `toggleDagNode` from `../lib/dagOpenNodes.js` - already imported by
  the panel; `DagPictureSpan` from Task 4.
- Produces, in `ui-tui/src/lib/dagOpenNodes.ts`:

```ts
/** The toggle key a picture span opens, or `null` when it opens nothing. */
export const dagSpanToggleKey = (
  runId: string,
  span: DagPictureSpan,
  nodes: readonly DagRunNode[]
): string | null
```

**Why a helper rather than a render assertion:** `ink-testing-library` yields frames, not a
queryable tree, and this package has no `react-test-renderer` - verified, and adding one is out
under the no-new-dependency constraint. So the decision of *what a span opens* is pulled out of
the component into a pure function that can be tested, leaving the component with nothing but
the wiring. The wiring itself is checked by hand in a real terminal; say so in the merge
request rather than implying unit coverage it does not have.

- [ ] **Step 1: Write the failing tests**

Append to `ui-tui/src/__tests__/dagPanel.test.tsx` (or `dagOpenNodes`'s own file if one is
added later - `dagOpenNodes.ts` has no test file today):

```tsx
describe('dagSpanToggleKey', () => {
  const nodes = [
    { ...node('a', 'running', [], 'Coder'), promptTemplate: 'first line\nBODY' },
    node('b', 'pending', ['a'], 'Coder')
  ]

  it('opens the same block the node row opens', () => {
    // The box is the bigger target and the thing a reader is already looking at,
    // so it must not open a second, separate disclosure.
    expect(dagSpanToggleKey('dag-6', { kind: 'border', nodeId: 'a', text: '╭──╮' }, nodes)).toBe(
      dagNodeKey('dag-6', 'a')
    )
  })

  it('opens nothing for a wire span, which belongs to no node', () => {
    expect(dagSpanToggleKey('dag-6', { kind: 'wire', text: '──┬──' }, nodes)).toBeNull()
  })

  it('opens nothing for a node with no template, rather than swallowing the click', () => {
    // Such a box has nothing to reveal; a dead affordance that eats a click is
    // worse than no affordance.
    expect(dagSpanToggleKey('dag-6', { kind: 'label', nodeId: 'b', text: '2 ○ Coder' }, nodes)).toBeNull()
  })

  it('opens nothing for a node absent from the run', () => {
    expect(dagSpanToggleKey('dag-6', { kind: 'label', nodeId: 'ghost', text: 'x' }, nodes)).toBeNull()
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run src/__tests__/dagPanel.test.tsx -t 'dagSpanToggleKey'`
Expected: FAIL - `dagSpanToggleKey` is not exported.

- [ ] **Step 3: Implement**

Add the helper to `dagOpenNodes.ts`:

```ts
export const dagSpanToggleKey = (
  runId: string,
  span: DagPictureSpan,
  nodes: readonly DagRunNode[]
): string | null => {
  const node = span.nodeId ? nodes.find(item => item.id === span.nodeId) : undefined

  return node?.promptTemplate ? dagNodeKey(runId, node.id) : null
}
```

Then wrap each picture row's box spans in a `Box` carrying `onClick` when the helper returns a
key. Two constraints, both already documented in `dagPanel.tsx`:

- the handler goes on the `Box`, not the `Text` - only `Box` carries mouse props in this fork;
- it must call `event.stopImmediatePropagation?.()`, because `dispatchClick` bubbles through
  every ancestor handler and the transcript rows above this one toggle on it.

`NodeRow` should switch to the same helper so the row and the box can never disagree about
what is expandable.

- [ ] **Step 4: Run to verify they pass**

Run: `npx vitest run src/__tests__/dagPanel.test.tsx`
Expected: PASS.

- [ ] **Step 5: Check the click by hand**

`npm run build` from `ui-tui/`, start `raven tui`, run any multi-node DAG, then click a box in
the picture and confirm the same prompt block opens as when clicking that node's row. Unit
tests cover the decision, not the mouse plumbing.

- [ ] **Step 6: Stage**

```bash
git add ui-tui/src/components/dagPanel.tsx ui-tui/src/lib/dagOpenNodes.ts ui-tui/src/__tests__/dagPanel.test.tsx
```

---

### Task 7: `/dag <ordinal>`

**Files:**
- Modify: `ui-tui/src/app/slash/commands/dag.ts`
- Test: `ui-tui/src/__tests__/dagCommand.test.ts`

**Interfaces:**
- Consumes: the ordinal definition from Task 5 - the 1-based index into the run's `nodes` array.

- [ ] **Step 1: Write the failing tests**

Append to `ui-tui/src/__tests__/dagCommand.test.ts`:

```ts
describe('/dag <ordinal>', () => {
  it('takes the short number the picture shows', () => {
    const rpc = vi.fn(() =>
      Promise.resolve({
        node: { run_id: 'dag-1', node: 'b', output_chars: 0, output_truncated: false }
      })
    )
    turnController.reset()
    openRun()

    dagCmd.run('2', buildCtx(rpc), 'dag')

    return vi.waitFor(() =>
      expect(rpc).toHaveBeenCalledWith('dag.node', expect.objectContaining({ node: 'b', run_id: 'dag-1' }))
    )
  })

  it('rejects a ordinal past the end of the run rather than falling back to an id', () => {
    // The ordinal refers to the graph on screen. Searching earlier runs for a
    // node that happens to be named "9" would answer a question nobody asked.
    const sys = vi.fn()
    const rpc = vi.fn(() => Promise.resolve({}))
    turnController.reset()
    openRun()

    dagCmd.run('9', buildCtx(rpc, sys), 'dag')

    expect(rpc).not.toHaveBeenCalled()
    expect(sys.mock.calls.flat().join('\n')).toContain('9')
  })

  it('numbers the ids in the refresh listing, so the mapping is printed', () => {
    const sys = vi.fn()
    const rpc = vi.fn(() => Promise.resolve({ run: SNAPSHOT }))
    turnController.reset()
    openRun()

    dagCmd.run('', buildCtx(rpc, sys), 'dag')

    return vi.waitFor(() => expect(sys.mock.calls.flat().join('\n')).toContain('1 a, 2 b'))
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run src/__tests__/dagCommand.test.ts -t 'ordinal'`
Expected: FAIL - `2` is treated as a node id and rejected.

- [ ] **Step 3: Implement**

In `dag.ts`, before the existing id lookup:

```ts
const ordinal = /^\d+$/.test(node) ? Number(node) : null

if (ordinal !== null) {
  const latest = runs[runs.length - 1]
  const target = latest?.nodes[ordinal - 1]

  if (!target) {
    transcript.sys(`no node ${ordinal} in this turn's latest DAG run`)

    return
  }
  // ... then take the existing `dag.node` path with target.id and latest.runId
}
```

The ordinal addresses the turn's most recent run - the one whose panel is on screen, and the one
the id lookup already prefers when several are open. Change the refresh listing from
`` refreshed.nodes.map(item => item.id).join(', ') `` to
`` refreshed.nodes.map((item, index) => `${index + 1} ${item.id}`).join(', ') ``, and update
the header comment, which currently says a node id is the only keyboard route to a node.

- [ ] **Step 4: Run to verify they pass**

Run: `npx vitest run src/__tests__/dagCommand.test.ts`
Expected: PASS, 11 tests.

- [ ] **Step 5: Full verification**

From `ui-tui/`:

```bash
npx vitest run src/__tests__/dag
npm run type-check
npm run lint
npm run build
```

The build matters even though nothing imports it in tests: `raven tui` loads the prebuilt
`ui-tui/dist/entry.js`, so without it a manual check shows the old code.

- [ ] **Step 6: Stage**

```bash
git add ui-tui/src/app/slash/commands/dag.ts ui-tui/src/__tests__/dagCommand.test.ts
```

---

## Before pushing

Run the `mr-review-patterns` pre-submit sweep over `git diff origin/main...HEAD`. That is a
standing requirement in this repo and is not optional. Fix findings in separate commits and
record anything left unresolved in the merge request body.
