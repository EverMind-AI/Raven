# Trajectory Agent-First Waterfall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the trajectory page's single cost bar and one-scope row list with a waterfall grouped by agent, then turn, then span, on one gap-compressed time axis.

**Architecture:** Three new pure modules (`axis.ts`, `model.ts`, `segments.ts`) turn the entry stream into agents, turns and rows and into the geometry and colours of their bars. One new component tree (`Waterfall.tsx` plus `OverviewLanes.tsx`) renders them with the existing window arithmetic. `store.ts` grows a scope-independent drawn set and a selection that does not switch tabs. `DurationBar`, `geometry`, `Hover` and `EntryList` are removed.

**Tech Stack:** React 19 + TypeScript, vitest + happy-dom, plain CSS in `ui-web/src/features/trajectory/styles.css`, i18n catalogue at repo-root `i18n/messages.json`.

**Spec:** `docs/specs/2026-10-28-trajectory-agent-waterfall-design.md`

## Global Constraints

- All source comments and identifiers are English. CJK appears only as values in
  `i18n/messages.json` (a file that already carries CJK) and never in `.ts`/`.tsx`.
- Tests and type checks run from `ui-web/`: `npx vitest run <path>` and
  `npm run type-check`. Never run bare `pytest` here; this package is npm-only.
- Timestamps on the wire are ISO strings (`operation_start`, `operation_end`,
  `event_time`). Parse once with `Date.parse`; all internal times are epoch ms.
- Bar geometry uses `operation_start`/`operation_end`. `charged_ms` never touches
  geometry — it stays in the details pane.
- Every new catalogue key lands in `i18n/messages.json` under the flat `ui` object
  as `{"en": ..., "zh": ...}`, keyed `gui.trajectory.<area>.<name>`.
- Commits: Conventional Commits, English, header <= 100 chars, one per task.

## Review Focus

- A row whose end is far beyond its turn's end (a `subagent.run` that outlives the
  host turn) must not stretch the turn's bar or get clipped in its own agent's lane.
- An entry with a null or unresolvable `turn_span_id` must appear under its agent,
  never silently dropped.
- A long idle gap where every agent is idle must compress and be labelled; a gap
  where any agent is still running must not.
- A session with ~500 turns must keep the mounted row count bounded while scrolling.
- A hidden entry pointed at by a link must be revealed and drawn, not selected
  invisibly.

---

### Task 0: Worktree dependencies

**Files:** none.

**Interfaces:** none.

- [ ] **Step 1: Install the package's dependencies in the worktree**

```bash
cd ui-web && npm ci
```

Expected: `node_modules/` appears; `npx vitest run src/features/trajectory/order.test.ts`
passes.

- [ ] **Step 2: Confirm the baseline is green**

Run: `cd ui-web && npx vitest run src/features/trajectory`
Expected: PASS for every existing trajectory test.

---

### Task 1: The compressed time axis

**Files:**
- Create: `ui-web/src/features/trajectory/waterfall/axis.ts`
- Test: `ui-web/src/features/trajectory/waterfall/axis.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces:
  ```ts
  export const IDLE_MS = 60_000
  export interface Interval { start: number; end: number }
  export interface Gap extends Interval { from: number; to: number }
  export interface TimeAxis {
    start: number; end: number; length: number
    gaps: readonly Gap[]
    project(time: number): number
    unproject(position: number): number
  }
  export interface Viewport { from: number; to: number }
  export function createAxis(intervals: readonly Interval[], options?: { idleMs?: number; gapWidth?: number }): TimeAxis
  export function clampViewport(viewport: Viewport, length: number): Viewport
  export function zoomViewport(viewport: Viewport, factor: number, anchor: number, length: number): Viewport
  export function barOf(axis: TimeAxis, viewport: Viewport, start: number, end: number): { left: number; width: number } | null
  ```

- [ ] **Step 1: Write the failing tests**

```ts
// axis.test.ts
import { createAxis, barOf, zoomViewport, IDLE_MS } from './axis'

test('merges overlapping intervals into one occupied run', () => {
  const axis = createAxis([{ start: 0, end: 10 }, { start: 5, end: 20 }], { idleMs: 100 })
  expect(axis.length).toBe(20)
})

test('collapses a gap longer than the idle threshold to the gap width', () => {
  const axis = createAxis([{ start: 0, end: 1000 }, { start: 1000 + IDLE_MS * 10, end: 1000 + IDLE_MS * 10 + 1000 }], { gapWidth: 50 })
  expect(axis.length).toBe(1000 + 50 + 1000)
  expect(axis.gaps).toHaveLength(1)
  expect(axis.gaps[0]!.end - axis.gaps[0]!.start).toBe(IDLE_MS * 10)
})

test('keeps a gap shorter than the threshold at its true width', () => {
  const axis = createAxis([{ start: 0, end: 1000 }, { start: 2000, end: 3000 }], { idleMs: IDLE_MS })
  expect(axis.length).toBe(3000)
  expect(axis.gaps).toHaveLength(0)
})

test('a stretch covered by another agent is occupied, not a gap', () => {
  const axis = createAxis([
    { start: 0, end: 1000 },
    { start: 1000 + IDLE_MS * 10, end: 1000 + IDLE_MS * 10 + 1000 },
    { start: 1000, end: 1000 + IDLE_MS * 10 },
  ], { idleMs: IDLE_MS })
  expect(axis.gaps).toHaveLength(0)
})

test('project and unproject round-trip inside an occupied run', () => {
  const axis = createAxis([{ start: 0, end: 1000 }])
  expect(axis.unproject(axis.project(420))).toBeCloseTo(420, 6)
})

test('a time inside a collapsed gap maps inside the gap position', () => {
  const axis = createAxis([{ start: 0, end: 1000 }, { start: 1000 + IDLE_MS * 10, end: 1000 + IDLE_MS * 10 + 1000 }], { gapWidth: 50 })
  const p = axis.project(1000 + IDLE_MS * 5)
  expect(p).toBeGreaterThan(1000)
  expect(p).toBeLessThan(1050)
})

test('zoomViewport keeps the anchor at the same relative position', () => {
  const next = zoomViewport({ from: 0, to: 100 }, 2, 25, 100)
  expect(next.from).toBeCloseTo(12.5, 6)
  expect(next.to).toBeCloseTo(62.5, 6)
})

test('barOf clips a bar to the viewport and returns null outside it', () => {
  const axis = createAxis([{ start: 0, end: 1000 }])
  expect(barOf(axis, { from: 0, to: 1000 }, 200, 400)).toEqual({ left: 0.2, width: 0.2 })
  expect(barOf(axis, { from: 500, to: 1000 }, 0, 100)).toBeNull()
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/axis.test.ts`
Expected: FAIL — `Failed to resolve import "./axis"`.

- [ ] **Step 3: Implement `axis.ts`**

One module docstring line stating the model: a piecewise map from real time to a
position where idle stretches are collapsed. `createAxis` sorts intervals, merges
touching ones (`start <= previous.end`), records a `Gap` for every stretch longer
than `idleMs` (default `IDLE_MS`), and assigns each gap
`gapWidth = options.gapWidth ?? Math.max(1000, Math.min(15_000, activeLength / 50))`.
`project`/`unproject` binary-search the pieces and interpolate linearly; a time
inside a gap maps proportionally across the gap's collapsed width. `barOf` returns
`{ left, width }` as fractions of the viewport, clamped to it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/axis.test.ts`
Expected: PASS, 8 tests.

- [ ] **Step 5: Type-check and commit**

```bash
cd ui-web && npm run type-check
git add ui-web/src/features/trajectory/waterfall/axis.ts ui-web/src/features/trajectory/waterfall/axis.test.ts
git commit -m "feat(ui-web): add a piecewise time axis that collapses idle gaps"
```

---

### Task 2: The agent → turn → span model

**Files:**
- Create: `ui-web/src/features/trajectory/waterfall/model.ts`
- Test: `ui-web/src/features/trajectory/waterfall/model.test.ts`

**Interfaces:**
- Consumes: `TrajectoryEntry` from `../types`.
- Produces:
  ```ts
  export interface WaterfallRow { entry: TrajectoryEntry; depth: number }
  export interface WaterfallTurn {
    id: string; ordinal: number; preview: string | null
    start: number; end: number
    reportedFrom: string | null; failure: boolean
    rows: readonly WaterfallRow[]
  }
  export interface WaterfallAgent {
    scope: string | null; name: string
    start: number; end: number
    turns: readonly WaterfallTurn[]; loose: readonly WaterfallRow[]
  }
  export interface Waterfall { agents: readonly WaterfallAgent[] }
  export function entryStart(entry: TrajectoryEntry): number
  export function entryEnd(entry: TrajectoryEntry): number
  export function buildWaterfall(entries: readonly TrajectoryEntry[], now: number): Waterfall
  ```

- [ ] **Step 1: Write the failing tests**

Use a small helper that builds a `TrajectoryEntry` from a partial, so each test
states only the fields it means:

```ts
// model.test.ts
import { buildWaterfall } from './model'

const E = (over: Partial<TrajectoryEntry>): TrajectoryEntry => ({
  entry_id: 'e', revision: 1, kind: 'llm.output', span_name: 'llm.call', slot: 'llm.output',
  trace_id: 't', span_id: 's', parent_span_id: null, turn_span_id: null, turn_number: null,
  turn_start: false, origin: 'main', subagent_scope: null, opens_subagent_scope: null,
  dispatched_by_subagent_scope: null, sort_key: [], event_time: '2026-10-28T10:00:00.000Z',
  preview: null, operation_status: 'ok', status_evidence: [], failure_entry: false,
  integrity: [], operation_start: null, operation_end: null, duration_ms: null,
  charged_ms: null, timing_basis: 'unknown', duration_owner: null, meta: {}, ...over,
})

test('turns are numbered per agent, not by the wire turn_number', () => {
  const w = buildWaterfall([
    E({ entry_id: 'm1', span_id: 'a', turn_span_id: 'T1', turn_number: 1, operation_start: '2026-10-28T10:00:00.000Z', operation_end: '2026-10-28T10:00:10.000Z' }),
    E({ entry_id: 'c1', span_id: 'b', subagent_scope: 't:sub', turn_span_id: 'ST1', turn_number: 2, operation_start: '2026-10-28T10:00:05.000Z', operation_end: '2026-10-28T10:01:00.000Z' }),
    E({ entry_id: 'm2', span_id: 'c', turn_span_id: 'T2', turn_number: 3, operation_start: '2026-10-28T10:01:00.000Z', operation_end: '2026-10-28T10:01:10.000Z' }),
  ], Date.parse('2026-10-28T10:02:00.000Z'))
  const main = w.agents[0]!
  expect(main.turns.map((t) => t.ordinal)).toEqual([1, 2])
  expect(w.agents[1]!.turns.map((t) => t.ordinal)).toEqual([1])
})

test('a turn ends where its own last row ends, not where a child outlives it', () => {
  const w = buildWaterfall([
    E({ entry_id: 'm1', span_id: 'a', turn_span_id: 'T1', operation_start: '2026-10-28T10:00:00.000Z', operation_end: '2026-10-28T10:00:20.800Z' }),
    E({ entry_id: 'spawn', span_id: 'b', kind: 'tool.output', span_name: 'subagent.run', turn_span_id: 'T1', operation_start: '2026-10-28T10:00:16.000Z', operation_end: '2026-10-28T10:02:15.000Z' }),
    E({ entry_id: 'end', span_id: 'c', kind: 'turn.end', slot: 'turn.output', turn_span_id: 'T1', operation_start: '2026-10-28T10:00:20.700Z', operation_end: '2026-10-28T10:00:20.800Z' }),
  ], Date.parse('2026-10-28T10:03:00.000Z'))
  expect(w.agents[0]!.turns[0]!.end).toBe(Date.parse('2026-10-28T10:00:20.800Z'))
})

test('an entry with no turn lands under its agent as a loose row', () => {
  const w = buildWaterfall([
    E({ entry_id: 'bg', span_id: 'z', turn_span_id: null, operation_start: '2026-10-28T10:01:00.000Z', operation_end: '2026-10-28T10:01:21.000Z' }),
  ], Date.parse('2026-10-28T10:02:00.000Z'))
  expect(w.agents[0]!.loose.map((r) => r.entry.entry_id)).toEqual(['bg'])
})

test('depth follows parent_span_id inside a turn', () => {
  const w = buildWaterfall([
    E({ entry_id: 'root', span_id: 'r', turn_span_id: 'T1', operation_start: '2026-10-28T10:00:00.000Z', operation_end: '2026-10-28T10:00:10.000Z' }),
    E({ entry_id: 'mid', span_id: 'm', parent_span_id: 'r', turn_span_id: 'T1', operation_start: '2026-10-28T10:00:01.000Z', operation_end: '2026-10-28T10:00:02.000Z' }),
    E({ entry_id: 'leaf', span_id: 'l', parent_span_id: 'm', turn_span_id: 'T1', operation_start: '2026-10-28T10:00:01.100Z', operation_end: '2026-10-28T10:00:01.900Z' }),
  ], Date.parse('2026-10-28T10:01:00.000Z'))
  const rows = w.agents[0]!.turns[0]!.rows
  expect(rows.map((r) => [r.entry.entry_id, r.depth])).toEqual([['root', 0], ['mid', 1], ['leaf', 2]])
})

test('the host agent comes first and agents are ordered by first start', () => {
  const w = buildWaterfall([
    E({ entry_id: 'c', subagent_scope: 't:sub', span_id: 'b', operation_start: '2026-10-28T10:00:01.000Z', operation_end: '2026-10-28T10:00:02.000Z' }),
    E({ entry_id: 'm', span_id: 'a', operation_start: '2026-10-28T10:00:05.000Z', operation_end: '2026-10-28T10:00:06.000Z' }),
  ], Date.parse('2026-10-28T10:01:00.000Z'))
  expect(w.agents.map((a) => a.scope)).toEqual([null, 't:sub'])
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/model.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `model.ts`**

`entryStart`/`entryEnd` parse `operation_start`/`operation_end` and fall back to
`event_time` (and `now` for a missing end). Group by `subagent_scope ?? null`;
inside a group, bucket by `turn_span_id` (nulls and unknown ids go to `loose`),
order turns by their earliest row start, and assign `ordinal` from 1. A turn's
`start` is its earliest row start and its `end` is the `operation_end` of the row
whose `slot === 'turn.output'`, else its latest row end. `reportedFrom` is
`turn_start` rows' `dispatched_by_subagent_scope`. Depth comes from walking
`parent_span_id` inside the turn's own spans, capped at 32 hops. Agent `name` is
`t('gui.trajectory.scope.main')` for the host and `scopeTitle(door)` from
`../scopes` otherwise, where `door` is the row whose `opens_subagent_scope` is the
agent's scope.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/model.test.ts`
Expected: PASS, 5 tests.

- [ ] **Step 5: Type-check and commit**

```bash
cd ui-web && npm run type-check
git add ui-web/src/features/trajectory/waterfall/model.ts ui-web/src/features/trajectory/waterfall/model.test.ts
git commit -m "feat(ui-web): group trajectory entries by agent, then turn, then span"
```

---

### Task 3: Turn bars as composite activity

**Files:**
- Create: `ui-web/src/features/trajectory/waterfall/segments.ts`
- Test: `ui-web/src/features/trajectory/waterfall/segments.test.ts`

**Interfaces:**
- Consumes: `WaterfallTurn`, `WaterfallRow` from `./model`.
- Produces:
  ```ts
  export type ActivityKind = 'failure' | 'subagent' | 'model' | 'tool' | 'waiting' | 'input'
  export interface Segment { start: number; end: number; kind: ActivityKind }
  export function segmentOf(row: WaterfallRow): ActivityKind
  export function turnSegments(turn: WaterfallTurn, now: number): Segment[]
  ```
  `segmentOf` order: a failed row is `failure`; `opens_subagent_scope !== null` is
  `subagent`; `kind` starting `llm.` is `model`; `tool.` is `tool`; the turn's
  input row is `input`; everything else is `waiting`.

- [ ] **Step 1: Write the failing tests**

```ts
// segments.test.ts
import { turnSegments } from './segments'
// build turns with the same `E` helper as model.test.ts, or export it from a shared
// `waterfall/testdata.ts` if you prefer — but do not duplicate it in a third file.

test('a turn with model and tool rows paints both and fills the rest with waiting', () => {
  const turn = makeTurn([
    ['model', 0, 1000], ['tool', 2000, 2400],
  ], 0, 4000)
  expect(turnSegments(turn, 0)).toEqual([
    { start: 0, end: 1000, kind: 'model' },
    { start: 1000, end: 2000, kind: 'waiting' },
    { start: 2000, end: 2400, kind: 'tool' },
    { start: 2400, end: 4000, kind: 'waiting' },
  ])
})

test('overlapping sub-agent and model rows give the span to the sub-agent', () => {
  const turn = makeTurn([['model', 0, 3000], ['subagent', 1000, 2000]], 0, 3000)
  expect(turnSegments(turn, 0).map((s) => s.kind)).toContain('subagent')
  expect(turnSegments(turn, 0).find((s) => s.start === 1000)!.kind).toBe('subagent')
})

test('a failed row wins over everything it overlaps', () => {
  const turn = makeTurn([['model', 0, 3000], ['failure', 1500, 1600]], 0, 3000)
  expect(turnSegments(turn, 0).find((s) => s.start === 1500)!.kind).toBe('failure')
})

test('rows are clipped to the turn window', () => {
  const turn = makeTurn([['subagent', 500, 9000]], 0, 2000)
  expect(turnSegments(turn, 0).at(-1)!.end).toBe(2000)
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/segments.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `segments.ts`**

Collect the turn's row intervals with `segmentOf`, clip each to
`[turn.start, turn.end]`, drop `input`, then sweep boundary points with a counter
per kind and pick by priority `failure > subagent > model > tool > waiting`,
defaulting to `waiting` where nothing is active. Merge adjacent equal kinds.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/segments.test.ts`
Expected: PASS, 4 tests.

- [ ] **Step 5: Type-check and commit**

```bash
cd ui-web && npm run type-check
git add ui-web/src/features/trajectory/waterfall/segments.ts ui-web/src/features/trajectory/waterfall/segments.test.ts
git commit -m "feat(ui-web): paint a turn's bar from its children's activity"
```

---

### Task 4: A scope-independent drawn set and selection

**Files:**
- Modify: `ui-web/src/features/trajectory/store.ts` (`Prefs`, `derived()`, `select()`)
- Test: `ui-web/src/features/trajectory/store.test.ts`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  ```ts
  export interface Prefs { showHidden: boolean }
  // on TrajectoryState:
  shown: TrajectoryEntry[]
  shownIndex: Record<string, number>
  // new action:
  export function selectAcross(entryId: string | null, opts: { source: SelectSource }): void
  ```
  `selectAcross` sets `selectedId`/`selectedBy`/`selections` and adds to `revealed`
  when the row is hidden, exactly as `select` does, but never calls `enter()`.

- [ ] **Step 1: Write the failing tests**

```ts
// appended to store.test.ts
test('shown carries every scope; visible stays filtered to the tab', () => { /* seed two scopes, assert shown has both and visible has one */ })
test('selectAcross selects another scope without switching tabs', () => { /* assert scope unchanged, selectedId set */ })
test('selectAcross reveals a hidden entry', () => { /* assert revealed includes it and shownIndex has it */ })
test('stored prefs from the threshold era read as showHidden only', () => {
  localStorage.setItem(PREFS_KEY, JSON.stringify({ minChargedMs: 20, showHidden: true }))
  expect(readPrefs()).toEqual({ showHidden: true })
})
```

Write these against the existing helpers in `store.test.ts` (it already seeds a
store through the source seams); do not add a second harness.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui-web && npx vitest run src/features/trajectory/store.test.ts`
Expected: FAIL — `shown` undefined, `selectAcross` is not a function.

- [ ] **Step 3: Implement in `store.ts`**

`Prefs` loses `minChargedMs`; `DEFAULT_PREFS` becomes `{ showHidden: false }`;
`loadPrefs` ignores any `minChargedMs`/`hideShort` it finds. `derived()` builds
`shown` as every entry `drawnUnder(prefs, revealed)` regardless of `scope`, and
`shownIndex` beside it. `selectAcross` is `select` without the scope branch.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ui-web && npx vitest run src/features/trajectory/store.test.ts`
Expected: PASS. Existing tests that mention `minChargedMs` must be updated in the
same step — `DurationBar.test.tsx` and `duration` fixtures are removed in Task 7,
so keep those edits to `store.test.ts` only here.

- [ ] **Step 5: Type-check and commit**

```bash
cd ui-web && npm run type-check
git add ui-web/src/features/trajectory/store.ts ui-web/src/features/trajectory/store.test.ts
git commit -m "feat(ui-web): track every scope's drawn rows and select across scopes"
```

---

### Task 5: The waterfall rows

**Files:**
- Create: `ui-web/src/features/trajectory/waterfall/Waterfall.tsx`
- Test: `ui-web/src/features/trajectory/waterfall/Waterfall.test.tsx`
- Modify: `ui-web/src/features/trajectory/styles.css`

**Interfaces:**
- Consumes: `buildWaterfall` (Task 2), `createAxis`/`barOf`/`Viewport` (Task 1),
  `turnSegments` (Task 3), `store.shown`/`selectAcross` (Task 4),
  `details.openDetails` (existing).
- Produces:
  ```ts
  export const ROW_HEIGHT = 28
  export interface DisplayRow { kind: 'agent' | 'turn' | 'span'; key: string; agent: number; turn?: string; row?: WaterfallRow }
  export function displayRows(w: Waterfall, collapsedAgents: ReadonlySet<string>, openTurns: ReadonlySet<string>): DisplayRow[]
  export function Waterfall(): JSX.Element
  ```

- [ ] **Step 1: Write the failing tests**

```tsx
// Waterfall.test.tsx
test('renders one agent header per agent and one turn row per turn', () => { /* seed store; assert headings and turn labels */ })
test('a turn row is collapsed by default and reveals its spans when clicked', () => { /* assert no span rows, click, assert span rows */ })
test('displayRows is bounded for a 500-turn session', () => {
  const rows = displayRows(bigWaterfall(500), new Set(), new Set())
  expect(rows.length).toBe(500 + 1)
  // and windowOf() over it mounts at most a screenful
})
test('clicking a span selects it across scopes and opens the details pane', () => { /* assert store.selectedId and details.open */ })
test('a turn row shows the origin mark when a report started it', () => { /* assert the badge text */ })
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/Waterfall.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `Waterfall.tsx`**

`displayRows` flattens agents → turns → spans, honouring the two sets. The
component reads `store` and `lang` with `useSyncExternalStore`, builds the axis
from all shown rows' intervals, renders a ruler plus a windowed list
(`windowOf(scrollTop, height, rows.length)` from `../EntryList` until Task 7
removes it — copy the four constants locally instead so this task does not depend
on a file that dies). Each row draws its bar with `barOf`. Turn rows clamp their
segments to the turn window; span rows use their own interval. Selection goes
through `store.selectAcross` and `details.openDetails()`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/Waterfall.test.tsx`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add ui-web/src/features/trajectory/waterfall/Waterfall.tsx ui-web/src/features/trajectory/waterfall/Waterfall.test.tsx ui-web/src/features/trajectory/styles.css
git commit -m "feat(ui-web): render trajectory agents, turns and spans as a waterfall"
```

---

### Task 6: The overview lanes

**Files:**
- Create: `ui-web/src/features/trajectory/waterfall/OverviewLanes.tsx`
- Test: `ui-web/src/features/trajectory/waterfall/OverviewLanes.test.tsx`

**Interfaces:**
- Consumes: `Waterfall`, `TimeAxis`.
- Produces: `export function OverviewLanes(props: { waterfall: Waterfall; axis: TimeAxis; viewport: Viewport }): JSX.Element`

- [ ] **Step 1: Write the failing tests**

```tsx
test('draws one lane per agent, in the waterfall order', () => { /* assert lane count and names */ })
test('a lone point span still gets a visible minimum width', () => { /* assert style width is at least the minimum */ })
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/OverviewLanes.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement**

One row per agent: its name in the gutter and, in the track, one `<i>` per
contiguous active stretch, positioned with `barOf`. Minimum width 2px. Running
agents get the pulsing class already defined for the Scope Tabs.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/OverviewLanes.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui-web/src/features/trajectory/waterfall/OverviewLanes.tsx ui-web/src/features/trajectory/waterfall/OverviewLanes.test.tsx
git commit -m "feat(ui-web): add one overview lane per trajectory agent"
```

---

### Task 7: The two cross-agent rules

**Files:**
- Create: `ui-web/src/features/trajectory/waterfall/links.ts`
- Test: `ui-web/src/features/trajectory/waterfall/links.test.ts`
- Modify: `ui-web/src/features/trajectory/waterfall/Waterfall.tsx`

**Interfaces:**
- Consumes: `Waterfall` (Task 2), `DisplayRow` (Task 5).
- Produces:
  ```ts
  export interface Link { at: number; fromRow: number; toRow: number; kind: 'dispatch' | 'report' }
  export function crossLinks(rows: readonly DisplayRow[], w: Waterfall): Link[]
  ```
  `at` is a real timestamp; `fromRow`/`toRow` index into `rows`.

- [ ] **Step 1: Write the failing tests**

```ts
test('a dispatch is a rule from the spawning span down to the child agent head', () => { /* one link, kind 'dispatch', from the spawn row to the child head row */ })
test('a report is a rule from the child agent end up to the turn it started', () => { /* kind 'report' */ })
test('no rule is emitted when the child agent has no rows', () => { /* empty */ })
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/links.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `links.ts`, then draw them in `Waterfall.tsx`**

A dispatch link exists for every agent whose scope is opened by a row
(`opens_subagent_scope === scope`); `at` is that row's `entryStart`. A report link
exists for every turn with a non-null `reportedFrom`; `at` is the scope's last
row end. The component renders each as an absolutely positioned 1px dashed rule
at `barOf(axis, viewport, at, at)!.left`, spanning the two rows' vertical extent,
behind the rows.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/links.test.ts`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add ui-web/src/features/trajectory/waterfall/links.ts ui-web/src/features/trajectory/waterfall/links.test.ts ui-web/src/features/trajectory/waterfall/Waterfall.tsx
git commit -m "feat(ui-web): draw the dispatch and report rules between agents"
```

---

### Task 8: Wire the page and remove the bar

**Files:**
- Modify: `ui-web/src/features/trajectory/TrajectoryApp.tsx`
- Delete: `DurationBar.tsx`, `DurationBar.test.tsx`, `geometry.ts`, `geometry.test.ts`, `Hover.tsx`, `EntryList.tsx`, `EntryList.test.tsx`
- Modify: `ui-web/src/features/trajectory/TrajectoryApp.test.tsx`, `styles.css`
- Modify: `i18n/messages.json`

**Interfaces:**
- Consumes: everything above.
- Produces: `TrajectoryApp` renders `Status`, `OverviewLanes`, `Waterfall`, `Details`; `export const mib` moves to `Waterfall.tsx` (or a new `format.ts`) so `Status` keeps compiling.

- [ ] **Step 1: Move `mib`, then delete the old surfaces one at a time**

After each deletion run `npx vitest run src/features/trajectory` and fix the
imports it breaks; the suite must be green before the next deletion.

- [ ] **Step 2: Update `TrajectoryApp.test.tsx`**

It currently asserts the bar and the row list exist. Replace those assertions
with: an agent heading renders, a turn row renders, and the details pane still
opens for a selected entry.

- [ ] **Step 3: Add the catalogue keys**

At minimum: `gui.trajectory.agent.turns` (`{n} turns`), `gui.trajectory.agent.background`,
`gui.trajectory.turn.ordinal` (`Turn {n}`), `gui.trajectory.turn.from_report`
(`Started by {name}`), `gui.trajectory.legend.model|tool|subagent|waiting|failure`,
`gui.trajectory.axis.idle` (`{duration} idle`). Every key needs both `en` and `zh`.

- [ ] **Step 4: Run the whole package's tests and the type check**

Run: `cd ui-web && npx vitest run && npm run type-check`
Expected: PASS. If a trajectory test outside the folder breaks, fix it here —
do not leave a red suite for a later task.

Then open the page in both themes (`data-theme="dark"` on `<html>`) and check
that host turns, sub-agent turns and span kinds stay distinguishable; the
sub-agent ramp is a second hue, not a tint of the host's.

- [ ] **Step 5: Commit**

```bash
git add -A ui-web/src/features/trajectory i18n/messages.json
git commit -m "refactor(ui-web): replace the trajectory bar with the agent waterfall"
```

---

### Task 9: Scope Tabs as a locator, and the acceptance pass

**Files:**
- Modify: `ui-web/src/features/trajectory/ScopeTabs.tsx`, `scopes.ts`, `ScopeTabs.test.tsx`
- Test: `ui-web/src/features/trajectory/waterfall/acceptance.test.tsx`

**Interfaces:**
- Consumes: `Waterfall`, `store`.
- Produces: `scopes.locate(scopeId)` scrolls the waterfall to that agent and
  selects its door entry, without changing `store.scope`.

- [ ] **Step 1: Write the acceptance tests from the spec**

One test per acceptance line in the spec, named for it:

```tsx
test('a sub-agent outliving its turn draws past the turn end and the idle break')
test('the turn a report started names the agent that started it')
test('an idle break is labelled and is not compressed while any agent runs')
test('turn ordinals are contiguous within each agent')
test('work outside a turn appears under its agent')
test('the details pane opens the same blocks for a selected entry')
test('a 500-turn session mounts a bounded number of rows')
```

- [ ] **Step 2: Run them to verify they fail, then implement**

Run: `cd ui-web && npx vitest run src/features/trajectory/waterfall/acceptance.test.tsx`
Expected: FAIL where behaviour is missing; then make each pass.

- [ ] **Step 3: Full verification**

```bash
cd ui-web && npx vitest run && npm run type-check && npm run lint
```

Expected: all three pass with no warnings introduced by this branch.

- [ ] **Step 4: Commit**

```bash
git add -A ui-web/src/features/trajectory
git commit -m "feat(ui-web): make the scope tabs locate their agent in the waterfall"
```
