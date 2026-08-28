/** What dragging a pane by its header means, decided away from the pointer.
 *
 * Two questions, both pure so the gesture's edge cases are testable without a
 * pointer: which arrangement is the reader proposing by holding a pane HERE,
 * and where would each pane sit under an arrangement. The surface owns the
 * pointer, the lift and the animation; this owns the meaning.
 *
 * The proposal is computed against the panes' CURRENT rects on every move and
 * nothing is rearranged until the pane is dropped -- the drop indicator is the
 * whole preview. Live-reflowing the other panes mid-drag was rejected on
 * purpose: their sizes change across slots (a stacked pane is full-width
 * half-height, a side-by-side one the reverse), and animating a size change
 * under a drag either distorts the content with a scale or re-lays out a
 * conversation's whole DOM every frame. An indicator is a rectangle; it can
 * move at the frame rate whatever the panes hold.
 */

import type { DeskDuo, DeskSplits } from './deskTypes'

export interface SlotRect {
  left: number
  top: number
  width: number
  height: number
}

export interface DeskArrangement {
  order: string[]
  duo: DeskDuo
}

/* The stylesheet's --desk-half-gap, as the default: the surface measures the
   live value off the grid instead, because a media query narrows the gap on a
   small window and a constant here would drift from it. */
const HALF_GAP = 3

/* How far in from the grid's edge the two-pane orientation zones reach. Wide
   enough to hit without aiming, narrow enough to leave the middle of the other
   pane meaning "swap". */
const EDGE = 0.3

/* Where each pane of `order` sits, in the grid's own coordinates. One formula
   family for every count, mirroring the stylesheet's: the two-pane split
   percentages come from the same DeskSplits the dividers write, so the slot a
   drop indicator draws is the slot the pane will actually take. */
export function slotRects(
  count: number,
  duo: DeskDuo,
  splits: DeskSplits,
  width: number,
  height: number,
  halfGap: number = HALF_GAP,
): SlotRect[] {
  if (count <= 0) return []
  if (count === 1) return [{ left: 0, top: 0, width, height }]
  const col = (width * splits.column) / 100
  const leftRow = (height * splits.left) / 100
  const rightRow = (height * splits.right) / 100
  if (count === 2) {
    if (duo === 'cols') {
      return [
        { left: 0, top: 0, width: col - halfGap, height },
        { left: col + halfGap, top: 0, width: width - col - halfGap, height },
      ]
    }
    return [
      { left: 0, top: 0, width, height: leftRow - halfGap },
      { left: 0, top: leftRow + halfGap, width, height: height - leftRow - halfGap },
    ]
  }
  const leftColumn = [
    { left: 0, top: 0, width: col - halfGap, height: leftRow - halfGap },
    { left: 0, top: leftRow + halfGap, width: col - halfGap, height: height - leftRow - halfGap },
  ]
  if (count === 3) {
    return [...leftColumn, { left: col + halfGap, top: 0, width: width - col - halfGap, height }]
  }
  return [
    ...leftColumn,
    { left: col + halfGap, top: 0, width: width - col - halfGap, height: rightRow - halfGap },
    { left: col + halfGap, top: rightRow + halfGap, width: width - col - halfGap, height: height - rightRow - halfGap },
  ]
}

const inside = (r: SlotRect, x: number, y: number): boolean =>
  x >= r.left && x < r.left + r.width && y >= r.top && y < r.top + r.height

const same = (a: DeskArrangement, b: DeskArrangement): boolean =>
  a.duo === b.duo && a.order.length === b.order.length && a.order.every((id, i) => id === b.order[i])

/* The arrangement the reader is proposing by holding `draggedId` at (x, y),
 * or null for "what is already there" -- the caller treats null as no change,
 * which is also what keeps the proposal steady while the pointer crosses the
 * middle of the dragged pane's own slot.
 *
 * Two panes carry the whole vocabulary:
 * - the outer THIRDS of the grid, across the stacking axis, mean "turn the
 *   stack": a stacked pair dragged to the left or right edge becomes
 *   side-by-side with the dragged pane on that side, and a side-by-side pair
 *   dragged to the top or bottom edge becomes a stack. Checked first, because
 *   in a stack every x is inside one pane or the other, and a zone that lost
 *   to the swap test would be unreachable.
 * - the middle of the OTHER pane means "trade places", in either orientation.
 *
 * Three panes and four know only the trade: there is one layout per count, so
 * position is the only thing a drag can change, and the pane under the pointer
 * is the one being displaced.
 */
export function dragProposal(args: {
  arrangement: DeskArrangement
  draggedId: string
  x: number
  y: number
  grid: { width: number; height: number }
  rects: Map<string, SlotRect>
}): DeskArrangement | null {
  const { arrangement, draggedId, x, y, grid, rects } = args
  const { order, duo } = arrangement
  if (!order.includes(draggedId) || order.length < 2) return null
  const settle = (next: DeskArrangement): DeskArrangement | null => (same(next, arrangement) ? null : next)
  if (order.length === 2) {
    const other = order.find((id) => id !== draggedId) as string
    if (duo === 'rows') {
      if (grid.width > 0 && x <= grid.width * EDGE) return settle({ order: [draggedId, other], duo: 'cols' })
      if (grid.width > 0 && x >= grid.width * (1 - EDGE)) return settle({ order: [other, draggedId], duo: 'cols' })
    } else {
      if (grid.height > 0 && y <= grid.height * EDGE) return settle({ order: [draggedId, other], duo: 'rows' })
      if (grid.height > 0 && y >= grid.height * (1 - EDGE)) return settle({ order: [other, draggedId], duo: 'rows' })
    }
    const target = rects.get(other)
    /* Reversed, not rebuilt around the dragged pane: [other, dragged] is the
       identity whenever the dragged pane was already second. */
    if (target && inside(target, x, y)) return settle({ order: [order[1] as string, order[0] as string], duo })
    return null
  }
  for (const id of order) {
    if (id === draggedId) continue
    const target = rects.get(id)
    if (!target || !inside(target, x, y)) continue
    const next = order.map((held) => (held === draggedId ? id : held === id ? draggedId : held))
    return settle({ order: next, duo })
  }
  return null
}
