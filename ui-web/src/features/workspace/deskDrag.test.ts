/* The drag vocabulary: which arrangement holding a pane somewhere proposes,
 * and where each pane sits under one. */

import { describe, expect, it } from 'vitest'

import { dragProposal, slotRects } from './deskDrag'

import type { DeskArrangement, SlotRect } from './deskDrag'

const SPLITS = { column: 50, left: 50, right: 50 }
const GRID = { width: 800, height: 600 }

/* Rects the way the surface builds them: from the same slot arithmetic. */
function rectsOf(arrangement: DeskArrangement): Map<string, SlotRect> {
  const slots = slotRects(arrangement.order.length, arrangement.duo, SPLITS, GRID.width, GRID.height)
  return new Map(arrangement.order.map((id, index) => [id, slots[index] as SlotRect]))
}

const propose = (arrangement: DeskArrangement, draggedId: string, x: number, y: number): DeskArrangement | null =>
  dragProposal({ arrangement, draggedId, x, y, grid: GRID, rects: rectsOf(arrangement) })

describe('the slots', () => {
  it('cuts a stacked pair along the row split', () => {
    const [top, bottom] = slotRects(2, 'rows', { ...SPLITS, left: 40 }, 800, 600)
    expect(top).toEqual({ left: 0, top: 0, width: 800, height: 237 })
    expect(bottom).toEqual({ left: 0, top: 243, width: 800, height: 357 })
  })

  it('cuts a side-by-side pair along the column split', () => {
    const [left, right] = slotRects(2, 'cols', { ...SPLITS, column: 30 }, 800, 600)
    expect(left).toEqual({ left: 0, top: 0, width: 237, height: 600 })
    expect(right).toEqual({ left: 243, top: 0, width: 557, height: 600 })
  })

  it('takes the seam width it is handed', () => {
    /* The stylesheet narrows the gap on a small window (--desk-half-gap 2.5px
       under 1040px), and the surface measures it rather than assuming this
       file's default. */
    const [top, bottom] = slotRects(2, 'rows', SPLITS, 800, 600, 2.5)
    expect(top?.height).toBe(297.5)
    expect(bottom?.top).toBe(302.5)
  })

  it('gives the third pane the whole right column', () => {
    const rects = slotRects(3, 'rows', SPLITS, 800, 600)
    expect(rects[2]).toEqual({ left: 403, top: 0, width: 397, height: 600 })
    /* And the left column is stacked: same x, split heights. */
    expect(rects[0]?.left).toBe(0)
    expect(rects[1]?.top).toBe(303)
  })

  it('splits the right column for the fourth', () => {
    const rects = slotRects(4, 'rows', { ...SPLITS, right: 60 }, 800, 600)
    expect(rects[3]).toEqual({ left: 403, top: 363, width: 397, height: 237 })
  })
})

describe('what a drag proposes', () => {
  const stacked: DeskArrangement = { order: ['a', 'b'], duo: 'rows' }
  const paired: DeskArrangement = { order: ['a', 'b'], duo: 'cols' }

  it('swaps a stacked pair when the pane is held over the other one', () => {
    /* Middle of the lower pane, clear of both edge zones. */
    expect(propose(stacked, 'a', 400, 450)).toEqual({ order: ['b', 'a'], duo: 'rows' })
  })

  it('swaps a side-by-side pair the same way', () => {
    expect(propose(paired, 'b', 200, 300)).toEqual({ order: ['b', 'a'], duo: 'cols' })
  })

  it('turns a stack sideways when the pane is held at an edge', () => {
    /* Left edge: the held pane takes the left slot. The zone must win over the
       swap test, because in a stack every x sits inside one pane or the other. */
    expect(propose(stacked, 'b', 100, 450)).toEqual({ order: ['b', 'a'], duo: 'cols' })
    expect(propose(stacked, 'b', 700, 100)).toEqual({ order: ['a', 'b'], duo: 'cols' })
  })

  it('stacks a side-by-side pair when the pane is held at the top or bottom', () => {
    expect(propose(paired, 'b', 200, 100)).toEqual({ order: ['b', 'a'], duo: 'rows' })
    expect(propose(paired, 'a', 600, 550)).toEqual({ order: ['b', 'a'], duo: 'rows' })
  })

  it('proposes nothing from the middle of the pane\'s own slot', () => {
    /* Holding still means leaving alone: the top pane held over its own middle,
       clear of every zone. */
    expect(propose(stacked, 'a', 400, 150)).toBeNull()
    /* An edge-zone hold that reproduces the current arrangement is also not a
       proposal: the left pane held at the left edge changes nothing. */
    expect(propose(paired, 'a', 100, 300)).toBeNull()
  })

  it('trades places with the pane under the pointer, from three up', () => {
    const three: DeskArrangement = { order: ['a', 'b', 'c'], duo: 'rows' }
    /* Over the right column: c's slot. */
    expect(propose(three, 'a', 600, 300)).toEqual({ order: ['c', 'b', 'a'], duo: 'rows' })
    /* Over its own slot: nothing. */
    expect(propose(three, 'a', 200, 100)).toBeNull()
  })

  it('answers nothing for a pane it does not hold', () => {
    expect(propose(stacked, 'zz', 400, 450)).toBeNull()
  })
})
