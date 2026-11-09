/* The popover's grid: the window drawn as cells.
 *
 * The rule under test is the one that maps a window onto its cells: how many
 * there are, and which part each one belongs to.
 */
import { describe, expect, it } from 'vitest'

import { CELLS, ctxCells } from './ctxGrid'

const parts = (over: Partial<Record<'system' | 'tools' | 'conversation' | 'memory' | 'skills', number>>) => ({
  system: 0,
  tools: 0,
  conversation: 0,
  memory: 0,
  skills: 0,
  ...over,
})

describe('the context grid', () => {
  it('is empty for a window nobody has measured', () => {
    expect(ctxCells(null, 0, 200_000)).toEqual([])
    expect(ctxCells(null, 1000, 0)).toEqual([])
  })

  it('draws one cell per share of the window', () => {
    expect(ctxCells(null, 200_000, 200_000)).toHaveLength(CELLS)
    expect(ctxCells(null, 100_000, 200_000)).toHaveLength(CELLS / 2)
    expect(ctxCells(null, 1_000, 200_000)).toHaveLength(1)
  })

  it('never draws more cells than the window has', () => {
    expect(ctxCells(null, 400_000, 200_000)).toHaveLength(CELLS)
  })

  it('leaves the cells uncoloured while no turn has divided the window', () => {
    expect(ctxCells(null, 100_000, 200_000).every((c) => c.part === null)).toBe(true)
  })

  it('colours the cells by the part they belong to', () => {
    const cells = ctxCells(parts({ system: 25_000, tools: 25_000, conversation: 150_000 }), 200_000, 200_000)
    expect(cells.filter((c) => c.part === 'system')).toHaveLength(14)
    expect(cells.filter((c) => c.part === 'tools')).toHaveLength(14)
    expect(cells.filter((c) => c.part === 'conversation')).toHaveLength(84)
    /* And in the order the parts fill the window. */
    expect(cells[0]!.part).toBe('system')
    expect(cells[14]!.part).toBe('tools')
    expect(cells[28]!.part).toBe('conversation')
  })

  it('draws nothing for parts that are not there', () => {
    const cells = ctxCells(parts({ conversation: 100_000 }), 200_000, 200_000)
    expect(cells.every((c) => c.part === 'conversation')).toBe(true)
  })

  it('gives the parts cells that add up to the fill', () => {
    /* A fill of 88% is 98 cells, and the parts' cells are those 98: the block
       does not stop short of the fill, and it does not overrun it. */
    const cells = ctxCells(
      parts({ system: 4_200, tools: 12_800, conversation: 108_600, memory: 2_100, skills: 3_300 }),
      175_600,
      200_000,
    )
    expect(cells).toHaveLength(98)
  })

  it('keeps one cell for a part too small for one of its own', () => {
    const cells = ctxCells(parts({ system: 100, tools: 1_000, conversation: 100_000 }), 200_000, 200_000)
    expect(cells.filter((c) => c.part === 'system')).toHaveLength(1)
    expect(cells.filter((c) => c.part === 'tools')).toHaveLength(1)
    /* And the cell they cost comes off the part with the most slack. */
    expect(cells.filter((c) => c.part === 'conversation')).toHaveLength(110)
    expect(cells).toHaveLength(112)
  })

  it('gives the memory and the skills their own cells', () => {
    const cells = ctxCells(
      parts({ system: 25_000, tools: 25_000, conversation: 100_000, memory: 30_000, skills: 20_000 }),
      200_000,
      200_000,
    )
    expect(cells.filter((c) => c.part === 'memory')).toHaveLength(17)
    expect(cells.filter((c) => c.part === 'skills')).toHaveLength(11)
    expect(cells).toHaveLength(112)
  })
})
