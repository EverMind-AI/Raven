/* The duration bar's arithmetic: where each entry's block goes, how wide,
 * what the pointer is over, and how the view moves -- all of it on numbers,
 * none of it on the document, so every rule here is a property a test can
 * state over thousands of random inputs rather than a picture a reader has
 * to squint at.
 *
 * One coordinate model holds every layout together. Each entry owns a SLOT:
 * a positive duration's slot is its share of the fit, floored at the minimum
 * width, and grows with the scale; a zero or unknown duration's slot is the
 * minimum width at the fit and grows with the scale too, while the mark drawn
 * inside it stays the minimum width -- so zooming pulls zero-length events
 * apart without ever drawing one as if it lasted. The fit is the one layout
 * that must be exactly as wide as the bar, so it alone may gather neighbours
 * into dense blocks when there are more entries than minimum widths; every
 * zoomed layout gives every entry its own slot and simply overflows.
 *
 * Nothing is placed by arithmetic on coordinates across two layouts. A place
 * is a stable entry id and a fraction of that entry's slot (`Anchor`), read
 * off the old layout with `locate` and found again in the new one with
 * `position` -- the same for a zoom about the pointer, a pan, a dense block
 * spread open and rows arriving under a zoomed view.
 */

import type { TrajectoryEntry } from './types'

export const GAP = 1
export const MIN_W = 3
export const MAX_SCALE = 128
/** A pointer moving less than this is a click, not a drag. */
export const DRAG_PX = 4
/** How long a click waits for a second one before it is a click. */
export const DBL_MS = 250
export const BAR_H = 32

export interface Segment {
  id: string
  /** The milliseconds this entry is charged, or null when none are. */
  charged: number | null
  turn: number | null
  failure: boolean
  kind: string
  start: string | null
  end: string | null
  eventTime: string
  basis: TrajectoryEntry['timing_basis']
}

export interface Block {
  /** The entry's id, or the first entry's for a dense block. */
  id: string
  x: number
  /** The slot's width; what is drawn inside it is the renderer's to decide. */
  w: number
  charged: number | null
  turn: number | null
  failure: boolean
  kind: string
  /** The entries gathered into this block, in order, when it is dense. */
  ids?: string[]
  /** The recorded milliseconds of a dense block's entries. */
  sum?: number
  /** How many of a dense block's entries have no recorded duration. */
  unknown?: number
}

export interface Layout {
  blocks: Block[]
  /** The x of each turn boundary, drawn over the gap before the block it precedes. */
  dividers: number[]
  contentWidth: number
  /** Pixels per millisecond for the positive durations in this layout. */
  unit: number
  dense: boolean
}

export interface Anchor {
  /** A stable entry id, never a dense block's. */
  id: string
  /** How far into that entry's slot, 0..1. */
  frac: number
}

export interface Viewport {
  scale: number
  offset: number
  width: number
  fit: boolean
  /** The fit's unit when the reader first zoomed; what later scales multiply. */
  frozenUnit: number | null
  anchor: Anchor | null
}

export interface Summary {
  known: number
  unknownCount: number
  /** A turn's own entry and entries inside it are both charged, so the sum holds overlap. */
  overlap: boolean
}

export const initialViewport = (width: number): Viewport =>
  ({ scale: 1, offset: 0, width, fit: true, frozenUnit: null, anchor: null })

const EMPTY: Layout = { blocks: [], dividers: [], contentWidth: 0, unit: 0, dense: false }

/* ── from rows to segments ────────────────────────────────────────────── */

export function toSegments(entries: readonly TrajectoryEntry[]): Segment[] {
  return entries.map((e) => ({
    id: e.entry_id,
    charged: typeof e.charged_ms === 'number' && e.charged_ms >= 0 ? e.charged_ms : null,
    turn: typeof e.turn_number === 'number' ? e.turn_number : null,
    failure: e.failure_entry,
    kind: e.kind,
    start: e.operation_start ?? null,
    end: e.operation_end ?? null,
    eventTime: e.event_time,
    basis: e.timing_basis,
  }))
}

const positive = (s: Segment): boolean => s.charged !== null && s.charged > 0

/* A duration nobody recorded. A zero-length mark recorded as such is known. */
const unknown = (s: Segment): boolean => s.charged === null && s.basis !== 'zero'

/* ── slots ────────────────────────────────────────────────────────────── */

/* The proportion `a` (pixels per millisecond) at which the positive entries,
   each floored at MIN_W, together with `floors` entries at MIN_W, fill exactly
   `budget` pixels. Monotonic in `a`, so a bisection finds it. */
function solveUnit(durations: number[], floors: number, budget: number): number {
  const need = (a: number): number => floors * MIN_W + durations.reduce((n, d) => n + Math.max(MIN_W, a * d), 0)
  if (!durations.length) return 0
  let lo = 0
  let hi = budget / Math.min(...durations)
  for (let i = 0; i < 64; i += 1) {
    const mid = (lo + hi) / 2
    if (need(mid) > budget) hi = mid
    else lo = mid
  }
  return lo
}

function place(segments: readonly Segment[], widths: number[], unit: number, dense: boolean): Layout {
  const blocks: Block[] = []
  const dividers: number[] = []
  let x = 0
  segments.forEach((s, i) => {
    if (i > 0 && s.turn !== segments[i - 1]!.turn) dividers.push(x - GAP)
    blocks.push({ id: s.id, x, w: widths[i]!, charged: s.charged, turn: s.turn, failure: s.failure, kind: s.kind })
    x += widths[i]! + GAP
  })
  return { blocks, dividers, contentWidth: blocks.length ? x - GAP : 0, unit, dense }
}

/* Every entry its own slot, the whole bar `width` wide: the positive
   durations share what the minimum marks leave in proportion. All zero or
   unknown, the width is shared equally. */
export function fitLayout(segments: readonly Segment[], width: number): Layout {
  const n = segments.length
  if (!n || width < MIN_W) return EMPTY
  const budget = Math.max(0, width - GAP * (n - 1))
  const durations = segments.filter(positive).map((s) => s.charged as number)
  const floors = n - durations.length
  if (!durations.length) {
    const each = budget / n
    return place(segments, segments.map(() => each), 0, false)
  }
  const unit = solveUnit(durations, floors, budget)
  const widths = segments.map((s) => (positive(s) ? Math.max(MIN_W, unit * (s.charged as number)) : MIN_W))
  /* Rounding the bisection leaves a fraction of a pixel; the last positive
     slot takes it so the bar is exactly as wide as asked. */
  const total = widths.reduce((a, b) => a + b, 0)
  const slack = budget - total
  if (Math.abs(slack) > 1e-6) {
    const last = segments.map(positive).lastIndexOf(true)
    widths[last] = Math.max(MIN_W, widths[last]! + slack)
  }
  return place(segments, widths, unit, false)
}

/* Past the fit: every entry its own slot, positive ones the frozen unit
   times the scale (floored), zero and unknown ones the minimum times the
   scale, and the bar as wide as that makes it. Rows arriving later extend it. */
export function spreadLayout(segments: readonly Segment[], unit: number, scale: number): Layout {
  if (!segments.length) return EMPTY
  const widths = segments.map((s) => (positive(s) ? Math.max(MIN_W, unit * scale * (s.charged as number)) : MIN_W * scale))
  return place(segments, widths, unit * scale, false)
}

/* ── density (the fit only) ───────────────────────────────────────────── */

/** How many slots of the minimum width, gaps included, fit in `width`; never fewer than one. */
export const capacity = (width: number): number => Math.max(1, Math.floor((width + GAP) / (MIN_W + GAP)))

/* Neighbours gathered into as many dense blocks as the width can show at the
   minimum, each block weighted by the sum of its entries' recorded durations
   (or equally, when none has one), then laid out like a fit of those. */
export function denseLayout(segments: readonly Segment[], width: number): Layout {
  const n = segments.length
  if (!n || width < MIN_W) return EMPTY
  const groupSize = Math.ceil(n / capacity(width))
  const groups: Segment[][] = []
  for (let i = 0; i < n; i += groupSize) groups.push(segments.slice(i, i + groupSize))
  const sums = groups.map((g) => g.filter(positive).reduce((a, s) => a + (s.charged as number), 0))
  const anyKnown = sums.some((v) => v > 0)
  const merged: Segment[] = groups.map((g, i) => ({
    id: g[0]!.id, charged: anyKnown ? (sums[i]! > 0 ? sums[i]! : null) : g.length, turn: g[0]!.turn,
    failure: g.some((s) => s.failure), kind: g[0]!.kind, start: null, end: null, eventTime: g[0]!.eventTime, basis: 'unknown',
  }))
  const base = fitLayout(merged, width)
  const blocks = base.blocks.map((b, i) => {
    const g = groups[i]!
    return { ...b, charged: sums[i]! > 0 ? sums[i]! : null, ids: g.map((s) => s.id), sum: sums[i]!, unknown: g.filter(unknown).length }
  })
  const dividers: number[] = []
  groups.forEach((g, i) => {
    if (i === 0) return
    const prev = groups[i - 1]!
    if (prev[prev.length - 1]!.turn !== g[0]!.turn) dividers.push(blocks[i]!.x - GAP)
  })
  return { ...base, blocks, dividers, dense: true }
}

/* ── the one entry point the bar draws from ───────────────────────────── */

/* The proportion the durations are drawn at before any zoom: the fit's own
   unit when the fit can give every entry its slot, and -- when there are
   more entries than minimum widths, so that no fit of them exists and a
   bisection would only find zero -- the plain share of the bar each recorded
   millisecond would have with no floors at all. Zero when nothing has a
   duration. */
export function referenceUnit(segments: readonly Segment[], width: number): number {
  if (!segments.length || width < MIN_W) return 0
  if (segments.length <= capacity(width)) return fitLayout(segments, width).unit
  const total = segments.filter(positive).reduce((n, s) => n + (s.charged as number), 0)
  return total > 0 ? width / total : 0
}

/* The unit a spread layout uses: the one frozen on the first zoom, or --
   while no entry had a duration to freeze one on -- the reference unit of
   the rows as they stand, so the first duration to arrive takes its
   proportion rather than the minimum width forever. */
export const unitFor = (segments: readonly Segment[], view: Viewport): number =>
  view.frozenUnit ?? referenceUnit(segments, view.width)

/* The unit to freeze when leaving the fit: the fit's own, unless there is
   none yet (no positive duration), in which case nothing is frozen and the
   first duration to arrive will set it. */
const freeze = (unit: number): number | null => (unit > 0 ? unit : null)

/* The layout for a viewport: the fit (dense when it must be), or the spread
   at the frozen unit and scale. Narrower than one minimum slot there is
   nothing that can be both as wide as the bar and as wide as a slot. */
export function layoutFor(segments: readonly Segment[], view: Viewport): Layout {
  if (!segments.length || view.width < MIN_W) return EMPTY
  if (view.fit) {
    return segments.length > capacity(view.width) ? denseLayout(segments, view.width) : fitLayout(segments, view.width)
  }
  return spreadLayout(segments, unitFor(segments, view), view.scale)
}

/* ── places ───────────────────────────────────────────────────────────── */

/** The block under content coordinate `x`; a gap belongs to the block before it. */
export function hitTest(layout: Layout, x: number): Block | null {
  const blocks = layout.blocks
  if (!blocks.length || x < 0 || x > layout.contentWidth) return null
  let lo = 0
  let hi = blocks.length - 1
  while (lo < hi) {
    const mid = (lo + hi + 1) >>> 1
    if (blocks[mid]!.x <= x) lo = mid
    else hi = mid - 1
  }
  return blocks[lo]!
}

/* The place at content coordinate `x`, as a stable entry and a fraction of
   its slot. Inside a dense block the fraction of the block is spread over its
   members in order, so a place keeps meaning when the block is opened up. */
export function locate(layout: Layout, x: number): Anchor | null {
  const block = hitTest(layout, x)
  if (!block) return null
  const frac = block.w > 0 ? Math.max(0, Math.min(1, (x - block.x) / block.w)) : 0
  if (!block.ids) return { id: block.id, frac }
  const n = block.ids.length
  const scaled = Math.min(frac * n, n - 1e-9)
  const idx = Math.floor(scaled)
  return { id: block.ids[idx]!, frac: scaled - idx }
}

/** The content coordinate of a place in this layout, or null when the entry is not in it. */
export function position(layout: Layout, anchor: Anchor): number | null {
  for (const block of layout.blocks) {
    if (block.id === anchor.id && !block.ids) return block.x + anchor.frac * block.w
    if (block.ids) {
      const idx = block.ids.indexOf(anchor.id)
      if (idx >= 0) return block.x + ((idx + anchor.frac) / block.ids.length) * block.w
    }
  }
  return null
}

const clampOffset = (offset: number, contentWidth: number, width: number): number =>
  Math.max(0, Math.min(offset, Math.max(0, contentWidth - width)))

/* The place at the end of the content, for a pointer past the last block. */
const lastPlace = (layout: Layout): Anchor | null => {
  const last = layout.blocks[layout.blocks.length - 1]
  if (!last) return null
  return last.ids ? { id: last.ids[last.ids.length - 1]!, frac: 1 } : { id: last.id, frac: 1 }
}

/* ── moves ────────────────────────────────────────────────────────────── */

/* A zoom about the pointer: the place under it in the layout before is put
   back under it in the layout after, then the offset clamps. Back at scale
   one the view is the fit again. */
export function zoomAt(view: Viewport, pointerX: number, factor: number, segments: readonly Segment[]): Viewport {
  if (!segments.length || view.width < MIN_W) return view
  const before = layoutFor(segments, view)
  const scale = Math.max(1, Math.min(MAX_SCALE, view.scale * factor))
  if (scale === 1) return { ...view, scale: 1, offset: 0, fit: true, anchor: null }
  const unit = unitFor(segments, view)
  const anchor = locate(before, pointerX + view.offset) ?? lastPlace(before)
  const after = spreadLayout(segments, unit, scale)
  const at = anchor ? position(after, anchor) : null
  const offset = clampOffset((at ?? 0) - pointerX, after.contentWidth, view.width)
  const next: Viewport = { ...view, scale, offset, fit: false, frozenUnit: view.frozenUnit ?? freeze(unit) }
  return { ...next, anchor: anchorOf(after, next) }
}

/** A drag of `dx` pixels (positive moves the content right, showing what was left). */
export function pan(view: Viewport, dx: number, contentWidth: number): Viewport {
  if (view.fit) return view
  return { ...view, offset: clampOffset(view.offset - dx, contentWidth, view.width) }
}

/** The place at the viewport's left edge. */
export const anchorOf = (layout: Layout, view: Viewport): Anchor | null =>
  locate(layout, Math.min(view.offset, layout.contentWidth))

/* After the rows changed under a zoomed view: the anchored place stays where
   it was on screen, or, when the entry is gone, the offset stays and clamps.
   A view that had no unit to freeze takes one now if the rows have grown a
   duration, so that duration is drawn in proportion from here on. */
export function restoreAnchor(layout: Layout, view: Viewport): Viewport {
  const frozenUnit = view.fit ? view.frozenUnit : (view.frozenUnit ?? freeze(layout.unit / Math.max(1, view.scale)))
  const keep = { ...view, frozenUnit, offset: clampOffset(view.offset, layout.contentWidth, view.width) }
  if (view.fit || !view.anchor) return keep
  const at = position(layout, view.anchor)
  if (at === null) return keep
  return { ...keep, offset: clampOffset(at, layout.contentWidth, view.width) }
}

/* "Spread this out": the zoom at which a dense block's entries fill the
   viewport as nearly as the scale allows, with the view starting on the
   block's first entry. Every entry has its own slot of at least the minimum
   in any spread layout; what the ceiling may deny is only that all of them
   fit the viewport at once, in which case the rest are a pan away. */
export function expand(view: Viewport, block: Block, segments: readonly Segment[]): Viewport {
  const ids = block.ids ?? [block.id]
  if (!segments.length || view.width < MIN_W) return view
  const frozen = unitFor(segments, view)
  const one = spreadLayout(segments, frozen, 1)
  const first = position(one, { id: ids[0]!, frac: 0 })
  const last = position(one, { id: ids[ids.length - 1]!, frac: 1 })
  if (first === null || last === null) return view
  const span = Math.max(1e-6, last - first)
  const scale = Math.max(1, Math.min(MAX_SCALE, Math.floor((view.width / span) * 100) / 100))
  const after = spreadLayout(segments, frozen, scale)
  const start = position(after, { id: ids[0]!, frac: 0 }) ?? 0
  const next: Viewport = {
    ...view, scale, fit: false, frozenUnit: view.frozenUnit ?? freeze(frozen), offset: clampOffset(start, after.contentWidth, view.width),
  }
  return { ...next, anchor: anchorOf(after, next) }
}

/* ── the sum the bar reports ──────────────────────────────────────────── */

/* Only recorded values are added; the rest are counted. A turn's own entry
   and the entries inside it are each charged in full, so when both are
   present the sum holds overlap and the bar says so. */
export function summarize(segments: readonly Segment[]): Summary {
  let known = 0
  let unknownCount = 0
  const turnsWithReply = new Set<number>()
  const turnsWithInner = new Set<number>()
  for (const s of segments) {
    if (s.charged === null) { if (unknown(s)) unknownCount += 1; continue }
    known += s.charged
    if (s.charged > 0 && s.turn !== null) {
      if (s.kind === 'agent.reply') turnsWithReply.add(s.turn)
      else turnsWithInner.add(s.turn)
    }
  }
  const overlap = [...turnsWithReply].some((t) => turnsWithInner.has(t))
  return { known, unknownCount, overlap }
}
