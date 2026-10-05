import { describe, expect, it } from 'vitest'

import {
  GAP, MAX_SCALE, MIN_W, anchorOf, capacity, denseLayout, expand, fitLayout, hitTest, initialViewport, layoutFor, locate, pan,
  position, referenceUnit, restoreAnchor, summarize, toSegments, zoomAt,
} from './geometry'

import type { Segment, Viewport } from './geometry'
import type { TrajectoryEntry } from './types'

/* A deterministic generator, so a failing property names the seed that found it. */
function rng(seed: number): () => number {
  let s = seed >>> 0
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0
    return s / 2 ** 32
  }
}

const seg = (id: string, charged: number | null, turn = 1, over: Partial<Segment> = {}): Segment => ({
  id, charged, turn, failure: false, kind: 'tool.output', start: null, end: null, eventTime: '2026-01-01T00:00:00Z',
  basis: charged === null ? 'unknown' : charged === 0 ? 'zero' : 'span_full', ...over,
})

function randomSegments(next: () => number, n: number, zeroOnly = false): Segment[] {
  const out: Segment[] = []
  let turn = 1
  for (let i = 0; i < n; i += 1) {
    if (next() < 0.1) turn += 1
    const r = next()
    const charged = zeroOnly ? (r < 0.5 ? 0 : null) : r < 0.2 ? null : r < 0.4 ? 0 : Math.floor(next() * 60_000)
    out.push(seg(`s${i}`, charged, turn))
  }
  return out
}

const near = (a: number, b: number, eps = 1e-6): boolean => Math.abs(a - b) <= eps

/* Every layout's blocks: slots at least the minimum, abutting with one gap. */
function wellFormed(layout: ReturnType<typeof fitLayout>): void {
  for (const b of layout.blocks) expect(b.w).toBeGreaterThanOrEqual(MIN_W - 1e-9)
  for (let i = 1; i < layout.blocks.length; i += 1) {
    expect(near(layout.blocks[i]!.x, layout.blocks[i - 1]!.x + layout.blocks[i - 1]!.w + GAP)).toBe(true)
  }
}

describe('the fit', () => {
  it('fills the width exactly, floors every slot, and keeps the positive ones in proportion', () => {
    const next = rng(7)
    for (let round = 0; round < 200; round += 1) {
      const n = 1 + Math.floor(next() * 60)
      const width = 200 + Math.floor(next() * 1800)
      const segments = randomSegments(next, n)
      if (n > capacity(width)) continue
      const layout = fitLayout(segments, width)
      expect(layout.blocks).toHaveLength(n)
      expect(near(layout.contentWidth, width, 1e-6)).toBe(true)
      wellFormed(layout)
      const positives = layout.blocks.filter((b) => b.charged !== null && b.charged > 0 && b.w > MIN_W + 1e-9)
      for (let i = 1; i < positives.length; i += 1) {
        const a = positives[i - 1]!
        const b = positives[i]!
        expect(near(a.w / (a.charged as number), b.w / (b.charged as number), 1e-6)).toBe(true)
      }
    }
  })

  it('shares the width equally when nothing has a duration, and marks zero and unknown at the minimum', () => {
    const layout = fitLayout([seg('a', null), seg('b', 0), seg('c', null)], 300)
    expect(layout.blocks.map((b) => b.w)).toEqual([(300 - 2 * GAP) / 3, (300 - 2 * GAP) / 3, (300 - 2 * GAP) / 3])
    const mixed = fitLayout([seg('a', 0), seg('b', 1000), seg('c', null)], 300)
    expect(mixed.blocks[0]!.w).toBe(MIN_W)
    expect(mixed.blocks[2]!.w).toBe(MIN_W)
    expect(near(mixed.blocks[1]!.w, 300 - 2 * GAP - 2 * MIN_W)).toBe(true)
  })

  it('draws a divider over the gap wherever the turn changes', () => {
    const layout = fitLayout([seg('a', 10, 1), seg('b', 10, 1), seg('c', 10, 2), seg('d', 10, 3), seg('e', 10, 3)], 500)
    expect(layout.dividers).toHaveLength(2)
    expect(near(layout.dividers[0]!, layout.blocks[2]!.x - GAP)).toBe(true)
    expect(near(layout.dividers[1]!, layout.blocks[3]!.x - GAP)).toBe(true)
  })

  it('is empty for no rows or a bar narrower than one slot, and one dense block as wide as the bar when it cannot hold two', () => {
    expect(layoutFor([], initialViewport(400)).blocks).toEqual([])
    expect(layoutFor([seg('a', 5)], initialViewport(0)).blocks).toEqual([])
    expect(layoutFor([seg('a', 5)], initialViewport(-10)).contentWidth).toBe(0)
    /* Two pixels cannot be both as wide as the bar and as wide as a slot. */
    expect(layoutFor([seg('a', 5), seg('b', 0)], initialViewport(MIN_W - 1)).blocks).toEqual([])
    const tiny = layoutFor([seg('a', 5), seg('b', null), seg('c', 0)], initialViewport(MIN_W))
    expect(capacity(MIN_W)).toBe(1)
    expect(tiny.dense).toBe(true)
    expect(tiny.blocks).toHaveLength(1)
    expect(tiny.blocks[0]!.ids).toEqual(['a', 'b', 'c'])
    expect(tiny.blocks[0]!.w).toBe(MIN_W)
    expect(tiny.contentWidth).toBe(MIN_W)
  })
})

describe('a dense fit', () => {
  /* Four hundred entries at two hundred pixels: fifty blocks of eight. */
  const width = 200
  const eight = (i: number): number | null => (i % 8 < 3 ? 0 : i % 8 < 5 ? null : 100 * (i % 8))

  it('counts a block\'s unknowns the way the sum does: the durations not recorded, never the recorded zeros', () => {
    const segments = Array.from({ length: 400 }, (_, i) => seg(`s${i}`, eight(i)))
    const dense = denseLayout(segments, width)
    expect(dense.blocks).toHaveLength(50)
    for (const b of dense.blocks) {
      expect(b.ids).toHaveLength(8)
      expect(b.unknown).toBe(2)
      expect(b.sum).toBe(500 + 600 + 700)
      expect(b.charged).toBe(500 + 600 + 700)
    }
    expect(dense.blocks.reduce((n, b) => n + b.unknown!, 0)).toBe(summarize(segments).unknownCount)
    const zeros = denseLayout(segments.map((s) => ({ ...s, charged: 0, basis: 'zero' as const })), width)
    for (const b of zeros.blocks) {
      expect(b.unknown).toBe(0)
      expect(b.sum).toBe(0)
      expect(b.charged).toBeNull()
    }
    const nulls = denseLayout(segments.map((s) => ({ ...s, charged: null, basis: 'unknown' as const })), width)
    for (const b of nulls.blocks) expect(b.unknown).toBe(b.ids!.length)
    expect(nulls.blocks.reduce((n, b) => n + b.unknown!, 0)).toBe(400)
  })

  it('has a unit of its own when no fit of the raw rows exists: the bar\'s share of each recorded millisecond', () => {
    const segments = Array.from({ length: 400 }, (_, i) => seg(`s${i}`, i % 10 === 0 ? 60_000 : 100))
    expect(fitLayout(segments, width).unit).toBe(0)
    const total = 40 * 60_000 + 360 * 100
    expect(near(referenceUnit(segments, width), width / total)).toBe(true)
    expect(referenceUnit(segments.map((s) => ({ ...s, charged: null, basis: 'unknown' as const })), width)).toBe(0)
    expect(referenceUnit([], width)).toBe(0)
    expect(referenceUnit(segments, MIN_W - 1)).toBe(0)
    /* Few enough rows for a fit: the fit's own unit, as before. */
    const few = segments.slice(0, 20)
    expect(referenceUnit(few, width)).toBe(fitLayout(few, width).unit)
  })
})

describe('the timing fixtures', () => {
  const rows = (over: Array<Partial<TrajectoryEntry> & Pick<TrajectoryEntry, 'entry_id'>>): TrajectoryEntry[] =>
    over.map((o) => ({
      revision: 1, kind: 'tool.output', span_name: 'tool.call', slot: 'tool.output', trace_id: 't',
      span_id: o.entry_id, parent_span_id: null, turn_span_id: 'turn', turn_number: 1, turn_start: false, origin: 'main',
      sort_key: [o.entry_id], event_time: '2026-01-01T00:00:00Z', preview: null, operation_status: 'ok',
      status_evidence: [], failure_entry: false, integrity: [], operation_start: null, operation_end: null,
      duration_ms: null, charged_ms: null, timing_basis: 'unknown', duration_owner: null, meta: {}, ...o,
    }))

  it('marks an input at zero width, a thinking entry as not recorded, and lets the output carry the call', () => {
    const segments = toSegments(rows([
      { entry_id: 'in', kind: 'llm.input', charged_ms: 0, timing_basis: 'zero' },
      { entry_id: 'think', kind: 'llm.thinking', charged_ms: null, timing_basis: 'not_recorded' },
      { entry_id: 'out', kind: 'llm.output', charged_ms: 3200, timing_basis: 'span_full' },
      { entry_id: 'lost', kind: 'tool.output', charged_ms: null, timing_basis: 'unknown' },
    ]))
    const layout = fitLayout(segments, 400)
    expect(layout.blocks.map((b) => b.w)).toEqual([MIN_W, MIN_W, 400 - 3 * GAP - 3 * MIN_W, MIN_W])
    const sum = summarize(segments)
    expect(sum.known).toBe(3200)
    /* The thinking entry and the one with no end are the unknowns; a zero is a value. */
    expect(sum.unknownCount).toBe(2)
    expect(sum.overlap).toBe(false)
  })

  it('adds parallel children and the turn that holds them, and says the sum holds overlap', () => {
    const segments = toSegments(rows([
      { entry_id: 'a', kind: 'tool.output', charged_ms: 2000, timing_basis: 'span_full', turn_number: 1 },
      { entry_id: 'b', kind: 'tool.output', charged_ms: 2000, timing_basis: 'span_full', turn_number: 1 },
      { entry_id: 'reply', kind: 'agent.reply', charged_ms: 4000, timing_basis: 'span_full', turn_number: 1 },
    ]))
    const sum = summarize(segments)
    expect(sum.known).toBe(8000)
    expect(sum.unknownCount).toBe(0)
    expect(sum.overlap).toBe(true)
  })
})

describe('places', () => {
  it('hit-tests without a seam, each gap going to the block before it', () => {
    const next = rng(11)
    for (let round = 0; round < 100; round += 1) {
      const segments = randomSegments(next, 1 + Math.floor(next() * 40))
      const width = 300 + Math.floor(next() * 900)
      const layout = layoutFor(segments, initialViewport(width))
      for (let x = 0; x <= layout.contentWidth; x += 0.5) {
        const hit = hitTest(layout, x)!
        expect(hit).not.toBeNull()
        expect(x).toBeGreaterThanOrEqual(hit.x - 1e-9)
        const nextBlock = layout.blocks[layout.blocks.indexOf(hit) + 1]
        if (nextBlock) expect(x).toBeLessThan(nextBlock.x)
      }
      expect(hitTest(layout, -1)).toBeNull()
      expect(hitTest(layout, layout.contentWidth + 1)).toBeNull()
    }
  })

  it('locates a place and finds it again in the same layout, dense or not', () => {
    const next = rng(23)
    for (let round = 0; round < 100; round += 1) {
      const n = 1 + Math.floor(next() * 600)
      const width = 50 + Math.floor(next() * 600)
      const segments = randomSegments(next, n, next() < 0.3)
      const view: Viewport = next() < 0.5
        ? initialViewport(width)
        : { ...initialViewport(width), fit: false, frozenUnit: fitLayout(segments, width).unit, scale: 1 + next() * 20 }
      const layout = layoutFor(segments, view)
      for (let i = 0; i < 20; i += 1) {
        const x = next() * layout.contentWidth
        const place = locate(layout, x)!
        expect(place).not.toBeNull()
        expect(segments.some((s) => s.id === place.id)).toBe(true)
        const back = position(layout, place)!
        /* Inside a dense block a place resolves to a member's share of it;
           a point in the gap after a block is the block's end. */
        const block = hitTest(layout, x)!
        const tolerance = (block.ids ? block.w / block.ids.length : 0) + GAP + 1e-6
        expect(Math.abs(back - x)).toBeLessThanOrEqual(tolerance)
      }
    }
  })

  it('keeps every entry in the same order when a dense fit is spread open', () => {
    const width = 200
    const segments = randomSegments(rng(5), 400)
    const dense = layoutFor(segments, initialViewport(width))
    expect(dense.dense).toBe(true)
    const spread = layoutFor(segments, { ...initialViewport(width), fit: false, frozenUnit: fitLayout(segments, width).unit, scale: 2 })
    expect(spread.dense).toBe(false)
    let prevDense = -1
    let prevSpread = -1
    for (const s of segments) {
      const a = position(dense, { id: s.id, frac: 0 })!
      const b = position(spread, { id: s.id, frac: 0 })!
      expect(a).toBeGreaterThanOrEqual(prevDense)
      expect(b).toBeGreaterThan(prevSpread)
      prevDense = a
      prevSpread = b
    }
    expect(dense.blocks.flatMap((b) => b.ids ?? [])).toEqual(segments.map((s) => s.id))
  })
})

describe('zoom', () => {
  it('keeps the point under the pointer when minimum marks and a positive block share the bar', () => {
    /* Twenty zero-length marks and one two-second call: the call starts at
       80px and takes the rest. Zooming twice about the pointer a sixth of
       the way into the call keeps that sixth under the pointer -- the slots
       before it have grown too, so the arithmetic is the place's, not a
       ratio of the whole. */
    const segments = [...Array.from({ length: 20 }, (_, i) => seg(`z${i}`, 0)), seg('call', 2000)]
    const width = 200
    const view = initialViewport(width)
    const before = layoutFor(segments, view)
    expect(near(before.blocks[20]!.x, 80)).toBe(true)
    expect(near(before.blocks[20]!.w, 120)).toBe(true)
    const pointer = 100
    const under = locate(before, pointer)!
    expect(under.id).toBe('call')
    expect(near(under.frac, 1 / 6)).toBe(true)
    const after = zoomAt(view, pointer, 2, segments)
    const layout = layoutFor(segments, after)
    const at = position(layout, under)!
    expect(near(at - after.offset, pointer, 1e-6)).toBe(true)
    /* The zero marks' slots doubled (the gaps between them did not): the call
       now starts at 20 * 6 + 20 = 140 and is 240 wide, so the sixth of it under
       the pointer sits at 180 and the view is offset by 80 to keep it at 100. */
    expect(near(layout.blocks[20]!.x, 140)).toBe(true)
    expect(near(layout.blocks[20]!.w, 240)).toBe(true)
    expect(near(after.offset, 80)).toBe(true)
  })

  it('keeps the place under the pointer through random zooms, within the limits', () => {
    const next = rng(21)
    for (let round = 0; round < 200; round += 1) {
      const segments = randomSegments(next, 2 + Math.floor(next() * 50), next() < 0.2)
      const width = 300 + Math.floor(next() * 900)
      let view = initialViewport(width)
      for (let step = 0; step < 6; step += 1) {
        const pointer = next() * width
        const factor = 0.5 + next() * 3
        const before = layoutFor(segments, view)
        const under = locate(before, pointer + view.offset)
        const after = zoomAt(view, pointer, factor, segments)
        expect(after.scale).toBeGreaterThanOrEqual(1)
        expect(after.scale).toBeLessThanOrEqual(MAX_SCALE)
        const layout = layoutFor(segments, after)
        wellFormed(layout)
        expect(after.offset).toBeGreaterThanOrEqual(0)
        expect(after.offset).toBeLessThanOrEqual(Math.max(0, layout.contentWidth - width) + 1e-6)
        if (after.fit) {
          expect(after.offset).toBe(0)
          expect(after.scale).toBe(1)
        } else if (under) {
          const at = position(layout, under)!
          const unclamped = at - pointer
          const clamped = Math.max(0, Math.min(unclamped, Math.max(0, layout.contentWidth - width)))
          /* Where the clamp did not bite, the pointer still names the place. */
          if (near(unclamped, clamped, 1e-6)) expect(near(at - after.offset, pointer, 1e-6)).toBe(true)
        }
        view = after
      }
    }
  })

  it('freezes the fit\'s unit on the first zoom, so later rows extend the bar instead of reshaping it', () => {
    const width = 600
    const segments = randomSegments(rng(9), 20)
    const zoomed = zoomAt(initialViewport(width), 300, 2, segments)
    expect(zoomed.frozenUnit).toBe(fitLayout(segments, width).unit)
    const before = layoutFor(segments, zoomed)
    const more = [...segments, seg('late', 5000, 9)]
    const after = layoutFor(more, zoomed)
    for (let i = 0; i < before.blocks.length; i += 1) {
      expect(near(after.blocks[i]!.x, before.blocks[i]!.x)).toBe(true)
      expect(near(after.blocks[i]!.w, before.blocks[i]!.w)).toBe(true)
    }
    expect(after.contentWidth).toBeGreaterThan(before.contentWidth)
    expect(near(layoutFor(more, initialViewport(width)).contentWidth, width)).toBe(true)
  })

  it('pulls zero-length marks apart when every entry is zero or unknown, with no unit to fit', () => {
    const width = 200
    const segments = randomSegments(rng(31), 400, true)
    const dense = layoutFor(segments, initialViewport(width))
    expect(dense.dense).toBe(true)
    expect(fitLayout(segments, width).unit).toBe(0)
    const zoomed = zoomAt(initialViewport(width), 100, 4, segments)
    expect(zoomed.frozenUnit).toBeNull()
    const spread = layoutFor(segments, zoomed)
    expect(spread.dense).toBe(false)
    expect(spread.blocks).toHaveLength(400)
    for (const b of spread.blocks) expect(near(b.w, MIN_W * zoomed.scale)).toBe(true)
    expect(spread.contentWidth).toBeGreaterThan(width)
  })

  it('zooms dense rows of recorded durations in proportion, so the long ones grow while the short stay marks', () => {
    const width = 200
    const segments = Array.from({ length: 400 }, (_, i) => seg(`s${i}`, i % 10 === 0 ? 60_000 : 100))
    expect(layoutFor(segments, initialViewport(width)).dense).toBe(true)
    const unit = referenceUnit(segments, width)
    const zoomed = zoomAt(initialViewport(width), 100, MAX_SCALE, segments)
    expect(zoomed.frozenUnit).toBe(unit)
    const spread = layoutFor(segments, zoomed)
    expect(spread.dense).toBe(false)
    const long = spread.blocks.find((b) => b.id === 's0')!
    const short = spread.blocks.find((b) => b.id === 's1')!
    expect(near(long.w, unit * MAX_SCALE * 60_000)).toBe(true)
    expect(long.w).toBeGreaterThan(MIN_W)
    expect(short.w).toBe(MIN_W)
    /* A second zoom multiplies the same unit; the proportion between two recorded durations holds. */
    const again = zoomAt(zoomAt(initialViewport(width), 100, 4, segments), 100, 4, segments)
    const twice = layoutFor(segments, again)
    const a = twice.blocks.find((b) => b.id === 's0')!
    const b = twice.blocks.find((b) => b.id === 's10')!
    expect(near(a.w, b.w)).toBe(true)
    expect(near(a.w, unit * 16 * 60_000)).toBe(true)
  })

  it('zooms mixed dense rows with the recorded ones in proportion and the marks at the minimum times the scale', () => {
    const width = 200
    const segments = randomSegments(rng(23), 400)
    expect(layoutFor(segments, initialViewport(width)).dense).toBe(true)
    const zoomed = zoomAt(initialViewport(width), 50, 8, segments)
    expect(zoomed.frozenUnit).toBe(referenceUnit(segments, width))
    expect(zoomed.frozenUnit).toBeGreaterThan(0)
    const spread = layoutFor(segments, zoomed)
    const bySeg = new Map(segments.map((s) => [s.id, s]))
    for (const b of spread.blocks) {
      const s = bySeg.get(b.id)!
      if (s.charged !== null && s.charged > 0) expect(near(b.w, Math.max(MIN_W, zoomed.frozenUnit! * 8 * s.charged))).toBe(true)
      else expect(near(b.w, MIN_W * 8)).toBe(true)
    }
  })

  it('gives dense rows that had no duration their proportion when the first recorded one arrives, keeping the place', () => {
    const width = 200
    const zeros = randomSegments(rng(31), 400, true)
    const zoomed = zoomAt(initialViewport(width), 100, 4, zeros)
    expect(zoomed.frozenUnit).toBeNull()
    const anchored = { ...zoomed, anchor: anchorOf(layoutFor(zeros, zoomed), zoomed) }
    expect(anchored.anchor).not.toBeNull()
    const grown = [...zeros, seg('late', 5000), seg('later', 50_000)]
    const relaid = layoutFor(grown, anchored)
    const restored = restoreAnchor(relaid, anchored)
    expect(restored.frozenUnit).toBe(referenceUnit(grown, width))
    expect(restored.frozenUnit).toBeGreaterThan(0)
    const later = relaid.blocks.find((b) => b.id === 'later')!
    expect(near(later.w, Math.max(MIN_W, restored.frozenUnit! * 4 * 50_000))).toBe(true)
    expect(near(restored.offset, Math.min(position(relaid, anchored.anchor!)!, relaid.contentWidth - width))).toBe(true)
  })

  it('gives the first duration to arrive under a zoom its proportion, freezing the unit then and keeping the anchor', () => {
    const width = 300
    const zeros = Array.from({ length: 10 }, (_, i) => seg(`z${i}`, 0))
    /* Zoomed while nothing had a duration: no unit is frozen. */
    let view = zoomAt(initialViewport(width), 150, 3, zeros)
    expect(view.frozenUnit).toBeNull()
    view = pan(view, -40, layoutFor(zeros, view).contentWidth)
    view = { ...view, anchor: anchorOf(layoutFor(zeros, view), view) }
    const anchorBefore = view.anchor!
    /* An output arrives with a duration. */
    const grown = [...zeros, seg('out', 4000)]
    const relaid = layoutFor(grown, view)
    const restored = restoreAnchor(relaid, view)
    expect(restored.frozenUnit).not.toBeNull()
    expect(restored.frozenUnit!).toBeGreaterThan(0)
    const settled = layoutFor(grown, restored)
    const out = settled.blocks.find((b) => b.id === 'out')!
    expect(out.w).toBeGreaterThan(MIN_W)
    /* The anchored mark is where it was on screen. */
    expect(near(position(settled, anchorBefore)! - restored.offset, position(layoutFor(zeros, view), anchorBefore)! - view.offset, 1e-6)).toBe(true)
    /* And from here the duration scales with the zoom. */
    const more = zoomAt(restored, 150, 2, grown)
    expect(more.frozenUnit).toBe(restored.frozenUnit)
    const bigger = layoutFor(grown, more).blocks.find((b) => b.id === 'out')!
    expect(near(bigger.w, out.w * 2, 1e-6)).toBe(true)
    /* The same when an existing entry's duration is filled in late. */
    const filled = zeros.map((z, i) => (i === 4 ? { ...z, charged: 2500, basis: 'span_full' as const } : z))
    const fromNull = restoreAnchor(layoutFor(filled, view), view)
    expect(fromNull.frozenUnit!).toBeGreaterThan(0)
    expect(layoutFor(filled, fromNull).blocks[4]!.w).toBeGreaterThan(MIN_W)
  })

  it('pans within the content and not at all while fitted; a zoom back to one is the fit again', () => {
    const width = 400
    const segments = randomSegments(rng(13), 30)
    const fitted = initialViewport(width)
    expect(pan(fitted, 50, 400)).toEqual(fitted)
    const zoomed = zoomAt(fitted, 100, 4, segments)
    const layout = layoutFor(segments, zoomed)
    expect(pan(zoomed, 10_000, layout.contentWidth).offset).toBe(0)
    expect(near(pan(zoomed, -10_000, layout.contentWidth).offset, layout.contentWidth - width)).toBe(true)
    const back = zoomAt(zoomed, 100, 0.01, segments)
    expect(back).toMatchObject({ fit: true, scale: 1, offset: 0 })
    expect(zoomAt(initialViewport(0), 10, 2, segments)).toEqual(initialViewport(0))
  })

  it('restores the anchored place to the viewport\'s left edge after the rows change', () => {
    const width = 400
    const segments = randomSegments(rng(17), 40)
    const zoomed = zoomAt(initialViewport(width), 200, 8, segments)
    const panned = pan(zoomed, -300, layoutFor(segments, zoomed).contentWidth)
    const layout = layoutFor(segments, panned)
    const anchor = anchorOf(layout, panned)!
    const view = { ...panned, anchor }
    const grown = [seg('early', 4000, 0), ...segments]
    const relaid = layoutFor(grown, view)
    const restored = restoreAnchor(relaid, view)
    expect(near(restored.offset, position(relaid, anchor)!, 1e-6)).toBe(true)
    const fewer = layoutFor(segments.slice(0, 5), view)
    expect(restoreAnchor(fewer, view).offset).toBeLessThanOrEqual(Math.max(0, fewer.contentWidth - width))
  })
})

describe('spreading a dense block out', () => {
  it('puts the block\'s first entry at the left edge, every entry in its own slot, as near the viewport\'s width as the scale allows', () => {
    const next = rng(5)
    for (const zeroOnly of [false, true]) {
      const width = 200
      const segments = randomSegments(next, 400, zeroOnly)
      const view = initialViewport(width)
      const dense = layoutFor(segments, view)
      expect(dense.dense).toBe(true)
      const bucket = dense.blocks[3]!
      const opened = expand(view, bucket, segments)
      expect(opened.fit).toBe(false)
      expect(opened.scale).toBeGreaterThanOrEqual(1)
      expect(opened.scale).toBeLessThanOrEqual(MAX_SCALE)
      const spread = layoutFor(segments, opened)
      expect(spread.dense).toBe(false)
      const ids = bucket.ids!
      const first = spread.blocks.find((b) => b.id === ids[0])!
      expect(near(first.x, opened.offset, 1e-6) || near(opened.offset, spread.contentWidth - width, 1e-6)).toBe(true)
      for (const id of ids) expect(spread.blocks.find((b) => b.id === id)!.w).toBeGreaterThanOrEqual(MIN_W - 1e-9)
      const last = spread.blocks.find((b) => b.id === ids[ids.length - 1])!
      const span = last.x + last.w - first.x
      /* The span fills the viewport, unless the scale ceiling or the floors stopped it short of exact. */
      expect(span).toBeLessThanOrEqual(width + ids.length * MIN_W + 1e-6)
    }
  })

  it('spreads a dense block of recorded durations out in proportion, not as equal marks', () => {
    const width = 200
    const segments = Array.from({ length: 400 }, (_, i) => seg(`s${i}`, 100 * (1 + (i % 8))))
    const view = initialViewport(width)
    const dense = layoutFor(segments, view)
    const bucket = dense.blocks[2]!
    expect(bucket.ids).toHaveLength(8)
    const opened = expand(view, bucket, segments)
    expect(opened.frozenUnit).toBe(referenceUnit(segments, width))
    const spread = layoutFor(segments, opened)
    const members = bucket.ids!.map((id) => spread.blocks.find((b) => b.id === id)!)
    expect(near(members[0]!.x, opened.offset)).toBe(true)
    expect(members[7]!.charged).toBe(800)
    expect(members[7]!.w).toBeGreaterThan(MIN_W)
    expect(near(members[7]!.w, opened.frozenUnit! * opened.scale * 800)).toBe(true)
    expect(members[0]!.w).toBe(MIN_W)
    /* Zooming on from there keeps growing the recorded ones. */
    const further = zoomAt(opened, 0, 4, segments)
    const grown = layoutFor(segments, further).blocks.find((b) => b.id === bucket.ids![7])!
    expect(near(grown.w, members[7]!.w * 4) || further.scale === MAX_SCALE).toBe(true)
    expect(grown.w).toBeGreaterThan(members[7]!.w)
  })

  it('at the ceiling, still leaves every entry its own slot and the block\'s first at the edge', () => {
    const width = 100
    const segments = randomSegments(rng(41), 5000, true)
    const view = initialViewport(width)
    const dense = layoutFor(segments, view)
    const bucket = dense.blocks[0]!
    expect(bucket.ids!.length).toBeGreaterThan(capacity(width))
    const opened = expand(view, bucket, segments)
    const spread = layoutFor(segments, opened)
    expect(spread.dense).toBe(false)
    expect(near(spread.blocks.find((b) => b.id === bucket.ids![0])!.x, opened.offset)).toBe(true)
    for (const b of spread.blocks) expect(b.w).toBeGreaterThanOrEqual(MIN_W)
    /* More members than the viewport can show at once: the rest are a pan away, not lost. */
    const shown = spread.blocks.filter((b) => b.x + b.w > opened.offset && b.x < opened.offset + width).length
    expect(shown).toBeLessThanOrEqual(bucket.ids!.length)
    expect(opened.scale).toBeLessThanOrEqual(MAX_SCALE)
  })
})
