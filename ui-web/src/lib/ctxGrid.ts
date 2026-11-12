/* The popover's grid: the window drawn as cells.
 *
 * A window is a number, and a number does not say what is inside it. The grid
 * is the window as a block of small squares -- a progress bar's idea, but with
 * the fill's composition in it: every cell is a slice of the window, and the
 * cells of one part wear that part's colour.
 *
 * The cells are the *filled* ones only. A window with room left in it shows a
 * block that stops short, the way a bar does, rather than a row of empty
 * squares that would have to be read as "empty of context" rather than "empty
 * of window".
 */

export type CtxPart = 'system' | 'tools' | 'conversation' | 'files' | 'memory' | 'skills'

export interface CtxParts {
  readonly system: number
  readonly tools: number
  readonly conversation: number
  readonly files: number
  readonly memory: number
  readonly skills: number
}

export interface CtxCell {
  /** The part this cell belongs to; null when no turn has divided the window. */
  readonly part: CtxPart | null
}

/** The parts, in the order they fill the window: what is fixed first, then the
    conversation with the files riding in it, and last what this turn recalled
    on top of it. */
export const PARTS: readonly CtxPart[] = ['system', 'tools', 'conversation', 'files', 'memory', 'skills']

/** The cells a full window is drawn with: a hundred and twelve, twenty-eight
    across. The stylesheet's grid has those twenty-eight columns, and the two
    have to agree. */
export const CELLS = 112

/**
 * How many cells each part's share of the fill comes to.
 *
 * Largest-remainder: every part's share is floored, and the cells left over go
 * to the parts with the biggest fractional shares, so the parts' cells add up
 * to exactly the fill -- a fill of 88% ends with a coloured cell in the 88th
 * hundredth of the block, not in the 84th.
 *
 * A part too small for a cell of its own keeps one all the same, so a part the
 * fill cannot show is still visible; the cell that costs comes off the part
 * with the most slack, which is the one that grew into the window.
 */
function cellsFor(parts: CtxParts, total: number, cells: number): number[] {
  const shares = PARTS.map((part) => {
    const tokens = parts[part] ?? 0
    const quota = total > 0 ? (tokens / total) * cells : 0
    const floor = Math.floor(quota)
    return { tokens, quota, cells: floor, rest: quota - floor }
  })
  for (const s of shares) {
    if (s.tokens > 0 && s.cells === 0) s.cells = 1
  }
  let sum = shares.reduce((n, s) => n + s.cells, 0)
  /* More parts than the fill has room for: the one with the most slack above
     its own share gives a cell back. */
  while (sum > cells) {
    const donor = shares
      .filter((s) => s.cells > 1)
      .sort((a, b) => (b.quota - b.cells) - (a.quota - a.cells))[0]
    if (!donor) break
    donor.cells -= 1
    sum -= 1
  }
  /* The leftovers go to the parts with the biggest fractional shares, one each:
     the fractional shares add up to less than the number of parts, so no part
     takes more than one of them. */
  const order = [...shares].sort((a, b) => b.rest - a.rest)
  for (const s of order) {
    if (sum >= cells) break
    s.cells += 1
    sum += 1
  }
  return shares.map((s) => s.cells)
}

/**
 * The window as cells: the filled ones, each with the part it belongs to.
 *
 * `parts` is null while no turn has divided the window, and then the cells
 * carry no part -- one colour for a window nobody has divided.
 */
export function ctxCells(parts: CtxParts | null, used: number, max: number): CtxCell[] {
  if (max <= 0 || used <= 0) return []
  const filled = Math.min(CELLS, Math.max(1, Math.round((CELLS * used) / max)))
  if (!parts) return Array.from({ length: filled }, () => ({ part: null }))
  const total = PARTS.reduce((sum, p) => sum + (parts[p] ?? 0), 0)
  if (total <= 0) return Array.from({ length: filled }, () => ({ part: null }))
  const counts = cellsFor(parts, total, filled)
  const cells: CtxCell[] = []
  PARTS.forEach((part, i) => {
    for (let k = 0; k < (counts[i] ?? 0); k++) cells.push({ part })
  })
  return cells.slice(0, filled)
}
