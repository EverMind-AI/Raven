// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { CARD, GAP_X, GAP_Y, H, PAD, SHEET, W, depths, gist, layout, layers, ordered, shape, summary, took } from './graph'

import type { Shell } from '../../shell/bridge'
import type { DagNode, DagRun } from './types'

/* T returns its key with the vars appended, so a test asserts which catalogue
   entry was chosen AND what was interpolated into it -- the legacy code did the
   interpolation by hand with .replace, so that is the part worth pinning. */
beforeEach(() => {
  const shell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    toast: () => {},
    menuAt: () => {},
    confirmAsk: () => {},
    showPage: () => {},
    dur: (ms) => `${ms}ms`,
  }
  window.RavenShell = shell
})

afterEach(() => {
  delete window.RavenShell
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
    const { width, height } = layout(nodes)
    expect(width).toBe(PAD * 2 + GAP_X + W)
    expect(height).toBe(PAD * 2 + 2 * H + (GAP_Y - H))
  })

  it('sizes from the dims it is handed, not from the sheet it defaults to', () => {
    /* The transcript's card draws the same graph smaller. Ignoring the argument
       is silent: the card would lay itself out at the sheet's geometry and run
       past its own edge, which no other assertion here would notice. */
    const nodes = [node('a'), node('b', ['a'])]
    expect(layout(nodes, CARD).width).toBe(CARD.PAD * 2 + CARD.GAP_X + CARD.W)
    expect(layout(nodes, SHEET).width).toBe(SHEET.PAD * 2 + SHEET.GAP_X + SHEET.W)
    expect(layout(nodes, CARD).width).toBeLessThan(layout(nodes, SHEET).width)
    /* And the default is the sheet, which is what every existing caller relies
       on by passing nothing. */
    expect(layout(nodes).width).toBe(layout(nodes, SHEET).width)
  })

  it('centres each column on the midline, so a fan-out reads as a diamond', () => {
    const nodes = [node('a'), node('b', ['a']), node('c', ['a']), node('m', ['b', 'c'])]
    const { at, height } = layout(nodes)
    /* The two single-node columns sit at the same y, halfway down; the pair
       straddles them. Stacked-from-the-top would have put all three at PAD. */
    expect(at.get('a')!.y).toBe(at.get('m')!.y)
    expect(at.get('a')!.y).toBe((height - H) / 2)
    expect(at.get('b')!.y).toBeLessThan(at.get('a')!.y)
    expect(at.get('c')!.y).toBeGreaterThan(at.get('a')!.y)
  })

  it('steps columns by GAP_X and rows by GAP_Y', () => {
    const nodes = [node('a'), node('b'), node('c', ['a'])]
    const { at } = layout(nodes)
    expect(at.get('b')!.y - at.get('a')!.y).toBe(GAP_Y)
    expect(at.get('c')!.x - at.get('a')!.x).toBe(GAP_X)
  })

  it('keeps the order the server sent inside a column', () => {
    const nodes = [node('second'), node('first')]
    const { at } = layout(nodes)
    expect(at.get('second')!.y).toBeLessThan(at.get('first')!.y)
  })

  it('sizes a single node without a negative gap', () => {
    const { width, height } = layout([node('only')])
    expect(width).toBe(PAD * 2 + W)
    expect(height).toBe(PAD * 2 + H)
  })

  it('does not fall over on an empty graph', () => {
    const { width, height, at } = layout([])
    expect(at.size).toBe(0)
    expect(width).toBeTypeOf('number')
    expect(height).toBeTypeOf('number')
  })
})

describe('dag sentences', () => {
  it('counts nodes and layers, and says parallel only when something is', () => {
    const wide = gist(run([node('a'), node('b', ['a']), node('c', ['a'])]))
    expect(wide).toContain('gui.dag.count {"n":3,"d":2}')
    expect(wide).toContain('gui.dag.parallel {"n":2}')
    expect(wide).not.toContain('gui.dag.serial')
    const line = gist(run([node('a'), node('b', ['a'])]))
    expect(line).toContain('gui.dag.serial')
    expect(line).not.toContain('gui.dag.parallel')
  })

  it('names each agent once, in first-seen order', () => {
    const g = gist(
      run([
        node('a', [], { subagent: 'Coder' }),
        node('b', ['a'], { subagent: 'Researcher' }),
        node('c', ['a'], { subagent: 'Coder' }),
      ]),
    )
    expect(g.endsWith('Coder · Researcher')).toBe(true)
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

describe('dag clock', () => {
  it('says nothing for a node that has not started', () => {
    expect(took(node('a'), 10_000)).toBe('')
  })

  it('measures a running node against now, and a finished one against its end', () => {
    expect(took(node('a', [], { started_at: 1000, status: 'running' }), 6000)).toBe('5000ms')
    expect(took(node('a', [], { started_at: 1000, ended_at: 3000 }), 999_999)).toBe('2000ms')
  })

  it('floors at one second, so a node inside a single tick does not read as zero', () => {
    expect(took(node('a', [], { started_at: 1000, ended_at: 1001 }), 1001)).toBe('1000ms')
  })

  /* The clock borrows the page's own formatter, which is optional on the Shell
     interface -- so the case worth pinning is a shell that does not publish it.
     It has to be loud: a blank string here reads as "this node has not started",
     which is a sentence the column already uses and means something else, so
     every clock in the graph would go quiet with nothing anywhere saying why.
     The one fake most likely to omit the verb is a test's. */
  it('refuses to blank the clock when the page has no formatter', () => {
    window.RavenShell = { ...window.RavenShell!, dur: undefined }
    expect(() => took(node('a', [], { started_at: 1000, ended_at: 3000 }), 9000)).toThrow(
      'RavenShell.dur is not wired',
    )
  })
})

describe('dag ordering', () => {
  it('walks the server order and drops an id the map does not hold', () => {
    const d = run([node('a'), node('b')])
    d.order.push('ghost')
    expect(ordered(d).map((n) => n.id)).toEqual(['a', 'b'])
  })
})
