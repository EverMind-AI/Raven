// @vitest-environment happy-dom
/* Stopping a turn, and the queue that was waiting behind it.
 *
 * Three separate guards keep a cancel from being undone by the server's own
 * unwinding, and only one of the two cancel replies releases the queue. None of
 * it was driven before: the phase machine is the island's and is tested there,
 * while the decisions about WHICH frame may release the queue live here, in the
 * page's turn layer. Characterisation, so the rewrite has a baseline.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/module-harness.mjs'
import * as turn from '../../features/composer/turn'

import type { Sources } from '../sources'

type Runtime = typeof import('./runtime')
type Registry = typeof import('./registry')
type Pipeline = typeof import('./pipeline')

interface Row { id: string; title?: string; status?: string | null; last?: string }

async function harness({ turnKept = true, rows = [{ id: 's1' }] as Row[] } = {}) {
  const log: unknown[][] = []
  const queue: string[] = []
  const asked: Array<[string, unknown]> = []
  let settleCancel: { res: () => void; rej: (e: unknown) => void } | null = null
  document.body.innerHTML = '<h1 id="title"></h1><div id="stage"></div><div id="flash"></div>'
  /* The modules under test are imported deepest first, which is the order that
     keeps one module graph: the fakes are installed around the modules the
     first import reaches, and one it did not is loaded afterwards without
     them. */
  await loadPart(async () => {
    await import('./runtime'); await import('./stages')
    return import('./pipeline')
  }, {
    fakes: {
      'src/state/page': { show: () => {} },
      'src/state/ws': { setOpen: () => {}, reset: () => {} },
      'src/state/session/conversation': {
        ask: (text: string) => log.push(['ask', text]),
        noteRow: (labelText: string) => log.push(['noteRow', labelText]),
        pitch: () => {},
        splitAtts: (t: string) => ({ text: t, atts: [] }),
        unpitch: () => {},
      },
      'src/features/rail/store': {
        markNew: () => {},
        draw: () => log.push(['sessionDraw']),
        endRename: () => {},
      },
      'src/state/sheetRack': { forget: () => {} },
      'src/state/session/rows': {
        sess: (id: string) => rows.find((r) => r.id === id),
        open: () => {},
        replace: () => {},
        rows: () => rows,
      },
      'src/features/composer/mount': {
        drawMeter: () => log.push(['drawMeter']),
        goPaint: () => log.push(['goState']),
        loadDraft: () => {},
        parkDraft: () => {},
        queueClear: () => { queue.length = 0 },
        queuePush: (text: string) => { queue.push(text) },
        queueRestore: (next: string[]) => { queue.length = 0; queue.push(...next) },
        queueShift: () => queue.shift(),
        queueSnapshot: () => [...queue],
        /* The island's own phase machine: what `cancellable` and `busy` mean is
           its answer, and the decisions under test read them. */
        turn,
        claimDraft: () => {},
      },
      'src/lib/duration': { formatDuration: (ms: number) => `${ms}ms` },
      'src/i18n/t': { t: (key: string) => key },
      'src/lib/dom': { $: looseQuery() },
      'src/lib/session': { current: () => 's1', setCurrent: (id: string | null) => log.push(['pointer', id]) },
      'src/features/rail/title': { plainTitle: (s: unknown) => String(s) },
      'src/state/toast': { show: (text: string) => log.push(['toast', text]) },
      'src/state/ctxChip': { set: () => {} },
      'src/lib/notifications': { show: () => {} },
      'src/state/banner': { draw: () => {} },
      'src/state/tier': { load: () => {} },
      'src/features/workspace/record': { wsOnHistory: () => {} },
      'src/features/rail/source': { rowPreview: (t: string) => t, touchSession: () => {} },
      'src/features/transcript/mount': {
        nudge: () => {},
        stopStream: () => {},
        finishTurn: () => log.push(['foldTurn']),
        artifacts: () => log.push(['artifacts']),
        turnKept: () => turnKept,
        killStatus: () => log.push(['killStatus']),
        step: () => ({ seal: () => {}, sayDelta: () => {}, thinkAppend: () => {} }),
        status: () => {},
      },
      'src/features/transcript/tail': {
        down: () => {},
      },
      'src/features/workspace/store': {
        currentTurn: () => 1,
        loadDeliveries: () => {},
      },
      'src/state/session/resume': {
        resume: () => {},
        refreshDag: () => {},
      },
    },
  })
  const runtime = (await import('./runtime')) as Runtime
  const registry = (await import('./registry')) as Registry
  const pipeline = (await import('./pipeline')) as Pipeline
  await fakeGateway((method: string, params: unknown) => {
    asked.push([method, params])
    if (method === 'turn.cancel') {
      return new Promise<void>((res, rej) => { settleCancel = { res: () => res(), rej } })
    }
    return Promise.resolve({})
  })
  const { setSources } = await import('../sources')
  setSources({ composer: { slash: [] }, rail: {}, transcript: {} } as unknown as Partial<Sources>)
  const part = await import('../../app/install')
  part.installActions()
  /* A turn is running on the open conversation, which is what a send records:
     the phase reducer is only reached for the conversation the page shows. */
  registry.adopt('s1')
  const tick = () => new Promise((r) => setTimeout(r, 0))
  return {
    runtime,
    registry,
    pipeline,
    log,
    queue,
    asked,
    rows,
    sent: () => asked.filter(([m]) => m === 'turn.send').map(([, p]) => (p as { content: string }).content),
    settle: () => { settleCancel!.res(); return tick() },
    refuse: () => { settleCancel!.rej(new Error('gone')); return tick() },
    did: (name: string) => log.filter((c) => c[0] === name),
    tick,
  }
}

afterEach(() => {
  vi.useRealTimers()
  turn._resetForTests()
})

/* N2: the three places a cancel is protected from the server's own unwinding.
   Miss any one of them and the queued send is released twice, or not at all. */
describe('a cancel in flight', () => {
  it('drops the server message.complete that finishes the cancelled turn', async () => {
    const h = await harness()
    turn.dispatch({ type: 'stream', cancellable: true })
    turn.dispatch({ type: 'cancel' })
    expect(turn.phase()).toBe('cancelling')
    h.queue.push('the queued one')

    h.pipeline.dispatch({ type: 'message.complete', payload: { usage: {} } })

    expect(h.did('foldTurn')).toEqual([])
    expect(h.queue).toEqual(['the queued one'])
    expect(h.sent()).toEqual([])
  })

  it('drops the cancelled error frame that answers our own cancel', async () => {
    const h = await harness()
    turn.dispatch({ type: 'stream', cancellable: true })
    turn.dispatch({ type: 'cancel' })
    h.queue.push('the queued one')

    h.pipeline.dispatch({ type: 'error', payload: { reason: 'cancelled_by_client' } })

    expect(turn.phase()).toBe('cancelling')
    expect(h.queue).toEqual(['the queued one'])
    expect(h.did('noteRow')).toEqual([])
  })

  it('folds the turn and releases the queue 400ms later when ANOTHER client cancelled', async () => {
    /* The cancel came from another window on the same conversation, so nothing
       here is in `cancelling` -- this frame is the only notice, and the delay is
       what lets the fold settle before the next send starts. */
    vi.useFakeTimers()
    const h = await harness()
    turn.dispatch({ type: 'stream', cancellable: true })
    h.queue.push('the queued one')

    h.pipeline.dispatch({ type: 'error', payload: { reason: 'cancelled_by_client' } })

    expect(h.did('foldTurn')).toHaveLength(1)
    expect(turn.phase()).toBe('idle')
    expect(h.sent()).toEqual([])

    await vi.advanceTimersByTimeAsync(400)

    expect(h.queue).toEqual([])
    expect(h.sent()).toEqual(['the queued one'])
  })
})

/* N3: the two cancel replies are NOT symmetric. */
describe('the reply to turn.cancel', () => {
  it('releases the queue when the cancel was accepted', async () => {
    const h = await harness()
    turn.dispatch({ type: 'stream', cancellable: true })
    h.queue.push('the queued one')

    h.runtime.stop()
    /* The fold is synchronous, and the phase stays `cancelling` until the reply. */
    expect(h.did('foldTurn')).toHaveLength(1)
    expect(turn.phase()).toBe('cancelling')
    expect(h.asked[0]).toEqual(['turn.cancel', { session_key: 's1' }])

    await h.settle()

    expect(h.queue).toEqual([])
    expect(h.sent()).toEqual(['the queued one'])
    /* The released send is the turn now. */
    expect(turn.phase()).toBe('sending')
  })

  it('leaves the queue alone when the cancel was refused', async () => {
    const h = await harness()
    turn.dispatch({ type: 'stream', cancellable: true })
    h.queue.push('the queued one')

    h.runtime.stop()
    await h.refuse()

    /* The turn is released either way -- the composer would stay locked
       otherwise -- but nothing is sent. */
    expect(turn.phase()).toBe('idle')
    expect(h.queue).toEqual(['the queued one'])
    expect(h.sent()).toEqual([])
  })

  it('does nothing at all for a turn the reader cannot stop', async () => {
    /* A runtime turn (a delegated result re-entering) is not cancellable, and
       the stop button claiming the UI would reset the stage under a stream
       still arriving. */
    const h = await harness()
    turn.dispatch({ type: 'stream', cancellable: false })

    h.runtime.stop()

    expect(h.asked).toEqual([])
    expect(h.did('foldTurn')).toEqual([])
    expect(turn.phase()).toBe('streaming')
  })
})

/* N4: the fold itself, and the promise its note makes. */
describe('folding a stopped turn', () => {
  it('keeps the phase in cancelling when the caller asked it to', async () => {
    const h = await harness()
    turn.dispatch({ type: 'stream', cancellable: true })
    turn.dispatch({ type: 'cancel' })

    h.runtime.softStop(true)
    expect(turn.phase()).toBe('cancelling')

    turn.dispatch({ type: 'stream', cancellable: true })
    h.runtime.softStop(false)
    expect(turn.phase()).toBe('idle')
  })

  it('only promises the output was kept when there is output above it', async () => {
    /* The fold runs where a seal would leave the prose as narration and then
       close over it -- the reader pressed stop and watched a half-written
       answer disappear under "done". */
    const kept = await harness({ turnKept: true })
    turn.dispatch({ type: 'stream', cancellable: true })
    kept.runtime.softStop(false)
    expect(kept.did('noteRow')).toEqual([['noteRow', 'gui.halted']])
    /* A stopped turn still produced what it produced. */
    expect(kept.did('artifacts')).toHaveLength(1)

    turn._resetForTests()
    const bare = await harness({ turnKept: false })
    turn.dispatch({ type: 'stream', cancellable: true })
    bare.runtime.softStop(false)
    expect(bare.did('noteRow')).toEqual([['noteRow', 'gui.halted_bare']])
  })
})

/* N5: the four places the queue moves, and the one place it is thrown away. */
describe('the send queue', () => {
  it('drains off the tail of a finished turn, with no busy check of its own', async () => {
    /* The idle dispatch two lines above is what makes that safe; the order is
       the whole of it. */
    const h = await harness({ rows: [{ id: 's1', title: 'a deck' }] })
    turn.dispatch({ type: 'stream', cancellable: true })
    h.queue.push('the queued one')

    h.pipeline.dispatch({ type: 'message.complete', payload: { usage: {} } })
    await h.tick()

    expect(h.queue).toEqual([])
    expect(h.sent()).toEqual(['the queued one'])
    /* The drain went through `liveSend`, so the released message is the turn
       now -- the idle dispatch that made the drain safe is already behind it. */
    expect(turn.phase()).toBe('sending')
  })

  it('refuses to drain while a turn is running', async () => {
    const h = await harness()
    turn.dispatch({ type: 'stream', cancellable: true })
    h.queue.push('the queued one')

    h.runtime.drain()

    expect(h.queue).toEqual(['the queued one'])
    expect(h.sent()).toEqual([])
  })

  it('sends into the running turn rather than queueing behind it', async () => {
    /* The queue is the fallback now, not the rule: a message typed mid-turn
       goes to the turn that is running (runtime.inject.test.ts drives the
       call and the fallback). */
    const h = await harness()
    turn.dispatch({ type: 'stream', cancellable: true })

    h.runtime.send('into the current one')
    await h.tick()

    expect(h.queue).toEqual([])
    expect(h.sent()).toEqual(['into the current one'])
  })

  it('is thrown away when the reader leaves for a new task', async () => {
    /* Any switch out drops it: a message typed for one conversation must not be
       sent to the next one. */
    const h = await harness()
    h.queue.push('never sent')

    h.registry.switchToDraft()

    expect(h.queue).toEqual([])
  })
})
