// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import * as pageStore from '../../state/page'
import { depths, layout, layers, ordered, shape, summary } from './graph'

import type { Dims } from './graph'
import type { DagNode, DagRun } from './types'

/* The translator returns its key with the vars appended, so a test asserts
   which catalogue entry was chosen AND what was interpolated into it. */
beforeEach(() => {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
})

afterEach(() => {
  resetTranslator()
})

const node = (id: string, deps: string[] = [], over: Partial<DagNode> = {}): DagNode => ({
  id,
  subagent: 'Researcher',
  instance: null,
  depends_on: deps,
  status: 'pending',
  started_at: null,
  ended_at: null,
  ...over,
})

const run = (nodes: DagNode[], over: Partial<DagRun> = {}): DagRun => ({
  run_id: 'r1',
  session: 's1',
  order: nodes.map((n) => n.id),
  nodes: new Map(nodes.map((n) => [n.id, n])),
  summary: null,
  done: false,
  folded: false,
  ...over,
})

const DIMS: Dims = { W: 184, H: 44, GAP_X: 230, GAP_Y: 60, PAD: 10 }

describe('dag depths', () => {
  it('puts a node in the column after the last thing it waits for', () => {
    /* Longest path, not shortest: `merge` waits on a node one layer deep, so it
       cannot sit beside it even though it also waits on a root. */
    const d = depths([node('a'), node('b', ['a']), node('merge', ['a', 'b'])])
    expect([...d]).toEqual([
      ['a', 0],
      ['b', 1],
      ['merge', 2],
    ])
  })

  it('treats a dependency that is not in the graph as no dependency', () => {
    const d = depths([node('only', ['ghost'])])
    expect(d.get('only')).toBe(0)
  })

  it('terminates on a cycle the server should never send', () => {
    const d = depths([node('x', ['y']), node('y', ['x'])])
    expect(d.get('x')).toBeTypeOf('number')
    expect(d.get('y')).toBeTypeOf('number')
  })

  it('terminates on a node that depends on itself', () => {
    /* One column in, not zero: the guard breaks the recursion by answering 0
       for the node already being walked, and the outer frame still adds its own
       step. Odd rather than wrong -- the point of the guard is that the panel
       draws something instead of hanging, and the server rejects a cyclic graph
       before it ever runs. Pinned so a future change to the guard has to be a
       deliberate one. */
    expect(depths([node('loop', ['loop'])]).get('loop')).toBe(1)
  })
})

describe('the shape of a graph', () => {
  it('counts one entry per layer, deepest last', () => {
    const nodes = [node('a'), node('b'), node('c', ['a', 'b']), node('d', ['c'])]
    expect(layers(nodes)).toEqual([2, 1, 1])
  })

  it('says how much work, how deep, and whether anything is side by side', () => {
    /* What the transcript's own row says, and the three facts that tell a chain
       from a fan-out -- the count alone told a reader neither. */
    expect(shape([node('a'), node('b'), node('c', ['a', 'b'])]))
      .toBe('gui.dag.count {"n":3,"d":2} · gui.dag.parallel {"n":2}')
    expect(shape([node('a'), node('b', ['a'])]))
      .toBe('gui.dag.count {"n":2,"d":2} · gui.dag.serial')
  })
})

describe('dag layout', () => {
  it('sizes the canvas from the widest column and the deepest path', () => {
    const nodes = [node('a'), node('b', ['a']), node('c', ['a'])]
    const { width, height } = layout(nodes, DIMS)
    expect(width).toBe(DIMS.PAD * 2 + DIMS.GAP_X + DIMS.W)
    expect(height).toBe(DIMS.PAD * 2 + 2 * DIMS.H + (DIMS.GAP_Y - DIMS.H))
  })

  it('centres each column on the midline, so a fan-out reads as a diamond', () => {
    const nodes = [node('a'), node('b', ['a']), node('c', ['a']), node('m', ['b', 'c'])]
    const { at, height } = layout(nodes, DIMS)
    /* The two single-node columns sit at the same y, halfway down; the pair
       straddles them. Stacked-from-the-top would have put all three at PAD. */
    expect(at.get('a')!.y).toBe(at.get('m')!.y)
    expect(at.get('a')!.y).toBe((height - DIMS.H) / 2)
    expect(at.get('b')!.y).toBeLessThan(at.get('a')!.y)
    expect(at.get('c')!.y).toBeGreaterThan(at.get('a')!.y)
  })

  it('steps columns by GAP_X and rows by GAP_Y', () => {
    const nodes = [node('a'), node('b'), node('c', ['a'])]
    const { at } = layout(nodes, DIMS)
    expect(at.get('b')!.y - at.get('a')!.y).toBe(DIMS.GAP_Y)
    expect(at.get('c')!.x - at.get('a')!.x).toBe(DIMS.GAP_X)
  })

  it('keeps the order the server sent inside a column', () => {
    const nodes = [node('second'), node('first')]
    const { at } = layout(nodes, DIMS)
    expect(at.get('second')!.y).toBeLessThan(at.get('first')!.y)
  })

  it('sizes a single node without a negative gap', () => {
    const { width, height } = layout([node('only')], DIMS)
    expect(width).toBe(DIMS.PAD * 2 + DIMS.W)
    expect(height).toBe(DIMS.PAD * 2 + DIMS.H)
  })

  it('does not fall over on an empty graph', () => {
    const { width, height, at } = layout([], DIMS)
    expect(at.size).toBe(0)
    expect(width).toBeTypeOf('number')
    expect(height).toBeTypeOf('number')
  })

  /* The task board reads top to bottom, because a pane docked beside a
     conversation has height to spend and not width. Same depths, same
     centring -- only the axis they count along differs. */
  describe('running downward', () => {
    it('puts a dependent below its upstream and siblings side by side', () => {
      const nodes = [node('a'), node('b', ['a']), node('c', ['a'])]
      const { at } = layout(nodes, DIMS, 'down')
      expect(at.get('b')!.y - at.get('a')!.y).toBe(DIMS.GAP_Y)
      expect(at.get('b')!.y).toBe(at.get('c')!.y)
      expect(at.get('c')!.x - at.get('b')!.x).toBe(DIMS.GAP_X)
    })

    it('centres each layer on the midline, so a fan-out still reads as a diamond', () => {
      const nodes = [node('a'), node('b', ['a']), node('c', ['a']), node('m', ['b', 'c'])]
      const { at, width } = layout(nodes, DIMS, 'down')
      expect(at.get('a')!.x).toBe(at.get('m')!.x)
      expect(at.get('a')!.x).toBe((width - DIMS.W) / 2)
      expect(at.get('b')!.x).toBeLessThan(at.get('a')!.x)
      expect(at.get('c')!.x).toBeGreaterThan(at.get('a')!.x)
    })

    /* A chain is the case the docked pane actually shows, and the one the
       sideways layout got wrong there: five steps across is a graph the board
       has to shrink to a third of life size before the reader sees it. */
    it('sizes a chain as one box wide and as deep as the chain', () => {
      const chain = [node('a'), node('b', ['a']), node('c', ['b'])]
      const down = layout(chain, DIMS, 'down')
      const across = layout(chain, DIMS)
      expect(down.width).toBe(DIMS.PAD * 2 + DIMS.W)
      expect(down.height).toBe(DIMS.PAD * 2 + 2 * DIMS.GAP_Y + DIMS.H)
      expect(down.width).toBeLessThan(across.width)
      expect(down.height).toBeGreaterThan(across.height)
    })

    it('leaves the default sideways, which is what every other surface draws', () => {
      const nodes = [node('a'), node('b', ['a'])]
      expect(layout(nodes, DIMS, 'across')).toEqual(layout(nodes, DIMS))
    })
  })
})

describe('dag sentences', () => {
  it('counts nodes and layers, and says parallel only when something is', () => {
    const wide = shape([node('a'), node('b', ['a']), node('c', ['a'])])
    expect(wide).toContain('gui.dag.count {"n":3,"d":2}')
    expect(wide).toContain('gui.dag.parallel {"n":2}')
    expect(wide).not.toContain('gui.dag.serial')
    const line = shape([node('a'), node('b', ['a'])])
    expect(line).toContain('gui.dag.serial')
    expect(line).not.toContain('gui.dag.parallel')
  })

  it('prefers the run report total over the graph length', () => {
    /* An interrupted run says how many nodes it meant to run; the graph only
       holds the ones the page heard about. */
    const d = run([node('a')], { summary: { completed: 1, total: 6 }, done: true })
    expect(summary(d)).toBe('gui.dag.done {"n":1,"t":6}')
  })

  it('falls back to the graph length when the report has no total', () => {
    const d = run([node('a'), node('b')], { summary: { completed: 2 }, done: true })
    expect(summary(d)).toBe('gui.dag.done {"n":2,"t":2}')
  })

  it('mentions failures and skips only when there are any', () => {
    const clean = summary(run([node('a')], { summary: { completed: 1, total: 1 } }))
    expect(clean).not.toContain('failed')
    expect(clean).not.toContain('skipped')
    const messy = summary(run([node('a')], { summary: { completed: 0, total: 3, failed: 2, skipped: 1 } }))
    expect(messy).toContain('gui.dag.failed {"n":2}')
    expect(messy).toContain('gui.dag.skipped {"n":1}')
  })

  it('reports a run with no summary at all as none done', () => {
    expect(summary(run([node('a')]))).toBe('gui.dag.done {"n":0,"t":1}')
  })
})

describe('dag ordering', () => {
  it('walks the server order and drops an id the map does not hold', () => {
    const d = run([node('a'), node('b')])
    d.order.push('ghost')
    expect(ordered(d).map((n) => n.id)).toEqual(['a', 'b'])
  })
})
