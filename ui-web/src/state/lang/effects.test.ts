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
import { describe, expect, it } from 'vitest'

import { loadPart } from '../../../scripts/module-harness.mjs'

/* What is left of the nineteen, in redrawAll's order. Six island steps went
   with the subscription each `<Domain>App` now holds on the language store:
   they were a bare `set({})`, which is what a re-render is. */
const ORDER = [
  'sessionDraw',
  'drawFoot',
  'modelLabel',
  'drawPerm',
  'drawCtx',
  'settings.redraw',
  'caps.draw',
  'nav.draw',
  'detail.close',
  'transcript.redraw',
  'queueDraw',
  'sessionOpen',
]

/* The three steps that are not a re-render, as the step's name in ORDER above,
   the module it really lives in, and the export it is called by: the settings
   dialog's epoch, the flyout's marks, the transcript's per-lane version bump. */
const ISLAND_STEPS: Array<[string, string, string]> = [
  ['settings.redraw', 'src/features/settings/store', 'redraw'],
  ['nav.draw', 'src/state/navfly', 'draw'],
  ['transcript.redraw', 'src/features/transcript/mount', 'redraw'],
]

interface Harness {
  order: string[]
  repaint(): void
  install(): void
}

/* Every step replaced by a recorder. `current` answers a session and the turn is
   idle, so the guarded reload at the end runs -- the one step of the list that
   can be skipped, and the case below that skips it is what says so. */
async function harness(
  { busy = false, draft = false, session = 'cli:one' as string | null, throwing = [] as string[] } = {},
): Promise<Harness> {
  const order: string[] = []
  const step = (name: string) => (): void => {
    if (throwing.includes(name)) throw new Error('not loaded')
    order.push(name)
  }
  const part = await loadPart(() => import('./effects'), {
    fakes: {
      ...Object.fromEntries(
        ISLAND_STEPS.map(([name, module, verb]) => [module, { [verb]: step(name) }])
      ),
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
  })
  return { order, repaint: part.repaint, install: part.install }
}

describe('the language repaint', () => {
  it('runs every step the whole-page redraw ran, in its order', async () => {
    const h = await harness()
    h.repaint()
    expect(h.order).toEqual(ORDER)
  })

  /* A page whose island has not loaded throws instead of drawing, and the
     redraw carries on: the capabilities page is the one step still guarded
     that way, because its two tabs register their renderers on first open. */
  it('carries on past a page whose island has not loaded', async () => {
    const h = await harness({ throwing: ['caps.draw'] })
    h.repaint()
    expect(h.order).toEqual(ORDER.filter((name) => name !== 'caps.draw'))
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
    expect(h.order).toEqual([])
    lang.set('en')
    expect(h.order).toEqual(ORDER)
  })
})
