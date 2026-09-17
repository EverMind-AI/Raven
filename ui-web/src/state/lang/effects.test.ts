// @vitest-environment happy-dom
/* The whole-page redraw a language pick asks for, and the order of it.
 *
 * Nineteen calls in a hand-written `redrawAll` became this, and the order was
 * never pinned by anything inside this directory: the gate on it
 * was a Python test outside ui-web that read the function's source text and
 * checked that every module page's renderer was named in it. That test goes with
 * the layer, so the order is asserted here instead -- as the sequence the calls
 * really run in, against fakes, which is more than the text check could say.
 *
 * One entry of the old list is absent: `drawCapsBadge` was an empty function
 * (the rail's module rows carry no counters), and it went with the draw shells.
 */
import { describe, expect, it, vi } from 'vitest'

import { loadPart } from '../../../scripts/module-harness.mjs'

/** The nineteen, in redrawAll's order, minus the empty one. */
const ORDER = [
  'sessionDraw',
  'drawFoot',
  'modelLabel',
  'drawPerm',
  'drawCtx',
  'settings.redraw',
  'caps.draw',
  'connections.redraw',
  'cron.redraw',
  'xa.redraw',
  'memory.redraw',
  'knowledge.redraw',
  'playbooks.redraw',
  'nav.draw',
  'detail.close',
  'transcript.redraw',
  'queueDraw',
  'sessionOpen',
]

/** The eight islands whose `redraw()` is a repaint in place, plus the two verbs. */
const ISLAND_STEPS: Array<[string, string]> = [
  ['settings', 'redraw'],
  ['connections', 'redraw'],
  ['cron', 'redraw'],
  ['xa', 'redraw'],
  ['memory', 'redraw'],
  ['knowledge', 'redraw'],
  ['playbooks', 'redraw'],
  ['nav', 'draw'],
  ['transcript', 'redraw'],
]

interface Harness {
  order: string[]
  repaint(): void
  install(): void
}

/* Every step replaced by a recorder. `current` answers a session and the turn is
   idle, so the guarded reload at the end runs -- the one step of the list that
   can be skipped, and the case below that skips it is what says so. */
async function harness({ busy = false, draft = false, session = 'cli:one' as string | null } = {}): Promise<Harness> {
  const order: string[] = []
  const step = (name: string) => () => { order.push(name) }
  const part = await loadPart(() => import('./effects'), {
    fakes: {
      'src/features/rail/store': {
        draw: step('sessionDraw'),
      },
      'src/state/session/rows': {
        sess: (id: string) => ({ id }),
        open: step('sessionOpen'),
      },
      'src/state/foot': { draw: step('drawFoot') },
      'src/state/perm': { draw: step('drawPerm') },
      'src/state/ctxChip': { draw: step('drawCtx') },
      'src/lib/session': { current: () => session },
      'src/features/model/chip': { label: step('modelLabel') },
      'src/features/composer/mount': { drawQueue: step('queueDraw'),
        turn: { busy: () => busy },
      },
      'src/state/caps': { draw: step('caps.draw') },
      'src/state/detail': { close: step('detail.close') },
      'src/state/session/registry': { isDraft: () => draft },
    },
    islands: Object.fromEntries(
      ISLAND_STEPS.map(([name, verb]) => [name, { [verb]: step(`${name}.${verb}`) }])
    ),
  })
  return { order, repaint: part.repaint, install: part.install }
}

describe('the language repaint', () => {
  it('runs every step the whole-page redraw ran, in its order', async () => {
    const h = await harness()
    h.repaint()
    expect(h.order).toEqual(ORDER)
  })

  /* Each module page, not just the open one: a hidden page keeps its old DOM,
     so it would still be in the previous language when reopened. An island that
     has not loaded throws instead, and the redraw carries on. */
  it('carries on past a page whose island has not loaded', async () => {
    const h = await harness()
    const { islands } = await import('../../features/registry')
    for (const name of ['cron', 'memory', 'playbooks'] as const) {
      vi.spyOn(islands[name], 'redraw').mockImplementation(() => { throw new Error('not loaded') })
    }
    h.repaint()
    expect(h.order).toEqual(ORDER.filter((name) => !/^(cron|memory|playbooks)\./.test(name)))
    vi.restoreAllMocks()
  })

  /* The reload rebuilds the conversation from disk, which would cut a streaming
     turn off; the transcript's own in-place redraw above is what covers it. */
  it('skips the reload while a turn is streaming, on a draft, and with no session', async () => {
    for (const over of [{ busy: true }, { draft: true }, { session: null }]) {
      const h = await harness(over)
      h.repaint()
      expect(h.order, JSON.stringify(over)).toEqual(ORDER.slice(0, -1))
    }
  })

  /* One subscriber rather than a call beside each pick, and on the group that
     runs after the regions have committed (state/lang/store.ts). */
  it('subscribes once, to the group that runs after the markup', async () => {
    const h = await harness()
    const lang = await import('./store')
    h.install()
    lang.setQuiet('zh')
    expect(h.order).toEqual([])
    lang.set('en')
    expect(h.order).toEqual(ORDER)
  })
})
