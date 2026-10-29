// @vitest-environment happy-dom
/* Stopping a turn, and the queue that was waiting behind it.
 *
 * Three separate guards keep a cancel from being undone by the server's own
 * unwinding, and only one of the two cancel replies releases the queue. None of
 * it was driven before: the phase machine is the island's and is tested there,
 * while the decisions about WHICH frame may release the queue live here, in the
 * page's turn layer. Characterisation, so the rewrite has a baseline.
 */

import { act } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/module-harness.mjs'
import * as turn from '../../features/composer/turn'

import type { Sources } from '../sources'

type Runtime = typeof import('./runtime')
type Registry = typeof import('./registry')
type Pipeline = typeof import('./pipeline')

interface Row { id: string; title?: string; status?: string | null; last?: string }

interface CancelReply { cancelled: boolean; busy?: boolean }

async function harness({ turnKept = true, rows = [{ id: 's1' }] as Row[] } = {}) {
  const log: unknown[][] = []
  const made: Array<{ text: string; sealed: boolean }> = []
  const queue: string[] = []
  const asked: Array<[string, unknown]> = []
  let settleCancel: { res: (reply: CancelReply) => void; rej: (e: unknown) => void } | null = null
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
        unask: (id: number) => log.push(['unask', id]),
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
        step: () => {
          const s = {
            text: '',
            sealed: false,
            seal: () => { s.sealed = true },
            sayDelta: (t: string) => { s.text += t },
            thinkAppend: (t: string) => { s.text += t },
          }
          made.push(s)
          return s
        },
        status: () => {},
      },
      'src/features/transcript/tail': {
        down: () => {},
      },
      'src/features/workspace/store': {
        advanceTurn: () => {},
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
      return new Promise<CancelReply>((res, rej) => { settleCancel = { res, rej } })
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
    settle: (reply: CancelReply = { cancelled: true }) => { settleCancel!.res(reply); return tick() },
    refuse: () => { settleCancel!.rej(new Error('gone')); return tick() },
    did: (name: string) => log.filter((c) => c[0] === name),
    steps: made,
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

/* A delta of the stopped turn can still be in flight when the cancel lands, and
   it opens a step of its own (`ensureStep`). The next turn must not write into
   that step, whichever frame opens it. */
describe('the turn after a stop', () => {
  type Frame = Parameters<Pipeline['dispatch']>[0]
  type Page = Awaited<ReturnType<typeof harness>>

  const OPENERS: Array<[string, Frame]> = [
    ['turn.started', { type: 'turn.started', payload: { turn_id: 'B' } }],
    ['message.start', { type: 'message.start', payload: { content: 'ask B', turn_id: 'B' } }],
  ]

  const stopOnLateFrame = async (h: Page, late: Frame, reply?: CancelReply): Promise<void> => {
    h.pipeline.dispatch({ type: 'message.start', payload: { content: 'ask A', turn_id: 'A' } })
    h.pipeline.dispatch({ type: 'token.delta', payload: { text: 'a1' } })
    h.runtime.stop()
    h.pipeline.dispatch(late)
    h.pipeline.dispatch({ type: 'message.complete', payload: { turn_id: 'A', usage: {} } })
    await h.settle(reply)
  }

  it.each(OPENERS)('starts on a clean step when %s follows a late delta', async (_name, opener) => {
    const h = await harness()
    await stopOnLateFrame(h, { type: 'token.delta', payload: { text: 'a-tail' } })

    h.pipeline.dispatch(opener)
    h.pipeline.dispatch({ type: 'token.delta', payload: { text: 'b1' } })

    expect(h.steps.map((s) => s.text)).toEqual(['a1', 'a-tail', 'b1'])
    /* The say buffer is what the turn's preview and notification read. */
    expect(h.runtime.state().say).toBe('b1')
    h.pipeline.dispatch({ type: 'message.complete', payload: { turn_id: 'B', usage: {} } })
    expect(h.rows[0]!.last).toBe('b1')
  })

  it.each(OPENERS)('starts on a clean step when %s ends a refusal that held the lane', async (_name, opener) => {
    /* The hold keeps the machine in `streaming` with no turn open, so the
       phase cannot be what tells a stale step from a live one. */
    const h = await harness()
    await stopOnLateFrame(h, { type: 'token.delta', payload: { text: 'a-tail' } }, { cancelled: false, busy: true })
    expect(turn.phase()).toBe('streaming')

    h.pipeline.dispatch(opener)
    h.pipeline.dispatch({ type: 'token.delta', payload: { text: 'b1' } })

    expect(h.steps.map((s) => s.text)).toEqual(['a1', 'a-tail', 'b1'])
    expect(h.runtime.state().say).toBe('b1')
  })

  it.each([
    ...OPENERS.map(([name, opener]): [string, (h: Page) => void] => [name, (h) => h.pipeline.dispatch(opener)]),
    ['a send', (h: Page) => h.runtime.send('ask B')],
  ])('seals a late thought when %s opens the next turn', async (_name, open) => {
    const h = await harness()
    await stopOnLateFrame(h, { type: 'thinking.delta', payload: { text: 'a-thought' } })
    expect(h.steps[1]).toMatchObject({ text: 'a-thought', sealed: false })

    open(h)

    expect(h.steps[1]!.sealed).toBe(true)
  })

  it('keeps the open step when a frame opens inside a turn still running', async () => {
    const h = await harness()
    h.pipeline.dispatch({ type: 'message.start', payload: { content: 'ask A', turn_id: 'A' } })
    h.pipeline.dispatch({ type: 'token.delta', payload: { text: 'a1' } })

    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 'B' } })
    h.pipeline.dispatch({ type: 'token.delta', payload: { text: 'b1' } })

    expect(h.steps.map((s) => s.text)).toEqual(['a1b1'])
    expect(h.steps[0]!.sealed).toBe(false)
    expect(h.runtime.state().say).toBe('a1b1')
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

  it('names a turn the runtime opened by the id its turn.started carried', async () => {
    /* No handle was bound for a delegated result re-entering, so the server
       reaches it only by id -- and only while it is still the turn running. */
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-reentry' } })

    h.runtime.stop()

    expect(h.asked[0]).toEqual(['turn.cancel', { session_key: 's1', turn_id: 't-reentry' }])
    await h.settle()
    /* The fold forgot it, so the next stop cannot name a turn that has ended. */
    expect(h.runtime.state().openedTurn).toBeNull()
  })

  it('sends no id for a turn a send opened', async () => {
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-reentry' } })
    h.pipeline.dispatch({ type: 'message.start', payload: { turn_id: 't-sent', content: 'next' } })

    h.runtime.stop()

    expect(h.asked[0]).toEqual(['turn.cancel', { session_key: 's1' }])
  })

  it('keeps a turn that opened after the press when the stale id is refused', async () => {
    /* The named turn finished on its own before the server read the cancel, and
       the next delegated result opened its own turn; the server refuses the
       stale id and leaves that one running. */
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-a' } })
    h.queue.push('the queued one')

    h.runtime.stop()
    h.pipeline.dispatch({ type: 'message.complete', payload: { usage: {} } })
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-b' } })
    await h.settle({ cancelled: false })

    expect(h.asked[0]).toEqual(['turn.cancel', { session_key: 's1', turn_id: 't-a' }])
    expect(turn.phase()).toBe('streaming')
    expect(turn.cancellable()).toBe(true)
    expect(h.runtime.state().openedTurn).toBe('t-b')
    expect(h.queue).toEqual(['the queued one'])
    expect(h.sent()).toEqual([])
  })

  it('keeps a sent turn that opened after the press when the stale id is refused', async () => {
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-a' } })
    h.queue.push('the queued one')

    h.runtime.stop()
    h.pipeline.dispatch({ type: 'message.start', payload: { turn_id: 't-sent', content: 'next' } })
    await h.settle({ cancelled: false })

    expect(turn.phase()).toBe('streaming')
    expect(turn.cancellable()).toBe(true)
    expect(h.sent()).toEqual([])
  })

  it('stays busy for a newer turn whose opening frame the refusal outran', async () => {
    /* The newer turn's first frame waits out the server's coalescing window and
       the reply does not, so the refusal can land first. It says the lane is
       still busy: going idle would release the queue under that turn. Nothing
       here can name the turn until its frame arrives, so it cannot be stopped
       before then. */
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-a' } })
    h.queue.push('the queued one')

    h.runtime.stop()
    h.pipeline.dispatch({ type: 'message.complete', payload: { usage: {} } })
    await h.settle({ cancelled: false, busy: true })

    expect(turn.phase()).toBe('streaming')
    expect(turn.cancellable()).toBe(false)
    expect(h.queue).toEqual(['the queued one'])
    expect(h.sent()).toEqual([])

    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-b' } })

    expect(turn.cancellable()).toBe(true)
    expect(h.runtime.state().openedTurn).toBe('t-b')
  })

  it('lets a busy conversation go when the turn it waited for ends without ever opening', async () => {
    /* A cron wake on the lane sends no opening frame, so nothing names it; the
       frame that ends it is the one every turn sends, and the queue goes out
       there. Every completion names the turn it belongs to, so the wake's
       carries its own id and not the stopped turn's. */
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-a' } })
    h.queue.push('the queued one')

    h.runtime.stop()
    h.pipeline.dispatch({ type: 'message.complete', payload: { turn_id: 't-a', usage: {} } })
    await h.settle({ cancelled: false, busy: true })
    expect(h.sent()).toEqual([])
    h.pipeline.dispatch({ type: 'message.complete', payload: { turn_id: 't-wake', usage: {} } })

    expect(h.sent()).toEqual(['the queued one'])
    expect(turn.phase()).toBe('sending')
  })

  it('stays busy when the refusal lands before the turn it stopped completes', async () => {
    /* The server read the cancel after the turn it named had already ended,
       so that turn's completion is on the way -- behind the refusal, which is
       written while the frames wait out the subscription's window. Folding it
       would release the queue into the turn that is starting, and the stop
       already folded the turn it stopped. */
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-a' } })
    h.queue.push('the queued one')

    h.runtime.stop()
    await h.settle({ cancelled: false, busy: true })
    expect(turn.phase()).toBe('streaming')
    expect(turn.cancellable()).toBe(false)
    const folds = h.did('foldTurn').length

    h.pipeline.dispatch({ type: 'message.complete', payload: { turn_id: 't-a', usage: {} } })

    expect(h.did('foldTurn')).toHaveLength(folds)
    expect(h.queue).toEqual(['the queued one'])
    expect(h.sent()).toEqual([])

    /* The turn that is starting opens: it names itself, and the queue waits
       for it. */
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-b' } })

    expect(turn.cancellable()).toBe(true)
    expect(h.sent()).toEqual([])
  })

  it('stays busy when a send opened the turn the refusal was about', async () => {
    /* The stop names no turn for one a send opened -- the server reaches it
       by the lane -- so the page has to know the id from the frame that
       opened it, to drop that turn's late completion. */
    const h = await harness()
    h.pipeline.dispatch({ type: 'message.start', payload: { turn_id: 't-sent', content: 'next' } })
    h.queue.push('the queued one')

    h.runtime.stop()
    expect(h.asked[0]).toEqual(['turn.cancel', { session_key: 's1' }])
    await h.settle({ cancelled: false, busy: true })

    h.pipeline.dispatch({ type: 'message.complete', payload: { turn_id: 't-sent', usage: {} } })

    expect(h.queue).toEqual(['the queued one'])
    expect(h.sent()).toEqual([])
  })

  it('drops the late unwind of the turn the hold was about', async () => {
    /* The turn the reader stopped is still unwinding when the refusal says
       the lane is busy: its notice is that turn's own news, and dropping it
       keeps the queue held for the turn that is starting. */
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-a' } })
    h.queue.push('the queued one')

    h.runtime.stop()
    await h.settle({ cancelled: false, busy: true })

    /* Fake timers only from here: the reply needed the real clock to settle. */
    vi.useFakeTimers()
    h.pipeline.dispatch({ type: 'error', payload: { reason: 'cancelled_by_client', turn_id: 't-a' } })
    await vi.advanceTimersByTimeAsync(400)

    expect(turn.phase()).toBe('streaming')
    expect(h.queue).toEqual(['the queued one'])
    expect(h.sent()).toEqual([])
  })

  it('lets a busy conversation go when another turn is cancelled while the hold is on', async () => {
    /* The turn the hold was waiting for was cancelled by another client: it
       is not coming, so the lane is free and the queue goes out. */
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-a' } })
    h.queue.push('the queued one')

    h.runtime.stop()
    await h.settle({ cancelled: false, busy: true })

    vi.useFakeTimers()
    h.pipeline.dispatch({ type: 'error', payload: { reason: 'cancelled_by_client', turn_id: 't-other' } })
    await vi.advanceTimersByTimeAsync(400)

    expect(h.sent()).toEqual(['the queued one'])
  })

  it('draws the question of the turn the hold was waiting for', async () => {
    /* The hold made the machine busy without a turn running in it, so the
       question that arrives belongs to the turn the hold was waiting for --
       and is drawn. */
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-a' } })
    h.queue.push('the queued one')

    h.runtime.stop()
    await h.settle({ cancelled: false, busy: true })

    h.pipeline.dispatch({ type: 'message.start', payload: { turn_id: 't-b', content: 'the next question' } })

    expect(h.did('ask')).toEqual([['ask', 'the next question']])
  })

  it('does not draw a question the machine already has once the hold is over', async () => {
    /* The hold is over: the turn it was waiting for ended, and the reader's
       queued message goes out under it. The machine is busy for that turn
       now, and its question is the bubble the reader typed -- drawing it
       again would double it. */
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-a' } })
    h.queue.push('the queued one')

    h.runtime.stop()
    await h.settle({ cancelled: false, busy: true })

    h.pipeline.dispatch({ type: 'message.complete', payload: { turn_id: 't-wake', usage: {} } })
    expect(h.sent()).toEqual(['the queued one'])
    expect(turn.phase()).toBe('sending')
    /* The reader's own message is on screen: the send drew it. */
    expect(h.did('ask')).toHaveLength(1)

    h.pipeline.dispatch({ type: 'message.start', payload: { turn_id: 't-queued', content: 'the queued one' } })

    expect(h.did('ask')).toHaveLength(1)
  })

  it('moves the bubble of a mid-turn message under the turn the hold was waiting for', async () => {
    /* The message was drawn inside the turn that was stopped; the turn that
       runs it as a turn of its own opens under the hold, and its bubble
       belongs at that turn's head. */
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-a' } })
    h.pipeline.dispatch({ type: 'message.injected', payload: { turn_id: 't-inj', content: 'steer' } })
    h.queue.push('the queued one')

    h.runtime.stop()
    await h.settle({ cancelled: false, busy: true })

    h.pipeline.dispatch({ type: 'message.start', payload: { turn_id: 't-inj', content: 'steer' } })

    expect(h.did('unask')).toHaveLength(1)
    expect(h.did('ask').map((c) => c[1])).toEqual(['steer', 'steer'])
  })

  /* A server older than the `busy` field says nothing about the lane, and that
     has to read the way a refusal always did. */
  const NOTHING_BEHIND: Array<[string, CancelReply]> = [
    ['says the lane is free', { cancelled: false, busy: false }],
    ['is older than the busy field', { cancelled: false }],
  ]

  it.each(NOTHING_BEHIND)('lets the conversation go when a refused id had nothing newer behind it: the server %s', async (_how, reply) => {
    /* Nothing was left to stop: the page must not sit in `cancelling`, and the
       engine is free, so the queue goes out as after a finished turn. */
    const h = await harness()
    h.pipeline.dispatch({ type: 'turn.started', payload: { turn_id: 't-a' } })
    h.queue.push('the queued one')

    h.runtime.stop()
    h.pipeline.dispatch({ type: 'message.complete', payload: { usage: {} } })
    await h.settle(reply)

    expect(h.sent()).toEqual(['the queued one'])
    expect(turn.phase()).toBe('sending')
  })

  it('does nothing at all for a turn the reader cannot stop', async () => {
    /* The machine's own guard, exercised with an uncancellable stream: while
       the phase is not one stop can act on, the button must neither call the
       server nor fold the stage under a stream still arriving. */
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

/* The page around a stop, whole: frames go through the pipeline, the stages and
   the runtime into the real transcript island, so what a turn lands as is read
   off the stage. Only what leaves the page is stood in for -- the OS
   notification, the rail's writers and the page switch. */
async function page() {
  ;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
  const pinged: unknown[][] = []
  const rows = [{ id: 's1', title: 'a chat' }]
  let settleCancel: ((reply: CancelReply) => void) | null = null
  document.body.innerHTML = '<h1 id="title"></h1><div id="scroll"><div class="col" id="stage"></div></div><div id="flash"></div>'
  await loadPart(async () => {
    await import('./runtime'); await import('./stages')
    return import('./pipeline')
  }, {
    fakes: {
      'src/lib/notifications': { show: (...a: unknown[]) => { pinged.push(a) } },
      'src/features/rail/source': { touchSession: () => {}, rowPreview: (t: string) => t },
      'src/features/rail/store': { draw: () => {}, markNew: () => {}, endRename: () => {} },
      'src/state/page': { show: () => {} },
    },
  })
  const { setTranslator } = await import('../../i18n/t')
  setTranslator((key) => key)
  const { setSources } = await import('../sources')
  setSources({
    transcript: { clean: (t: unknown) => String(t == null ? '' : t).trim(), okOf: () => true },
    rail: { snapshot: () => ({ rows }), replace: () => {}, open: () => {} },
    workspace: { shortPath: (p: string) => p, openPath: () => {} },
    prose: { pathOf: () => null, linkTargetOf: () => null },
    composer: { slash: [] },
  } as unknown as Partial<Sources>)
  const { installWsPane } = await import('../../test/wsPaneHarness')
  installWsPane()
  const { setCurrent } = await import('../../lib/session')
  setCurrent('s1')
  const registry = (await import('./registry')) as Registry
  registry.adopt('s1')
  registry.record('s1', 'sub-1')
  const pipeline = (await import('./pipeline')) as Pipeline
  const runtime = (await import('./runtime')) as Runtime
  const composer = await import('../../features/composer/mount')
  await fakeGateway((method: string) => {
    if (method === 'turn.cancel') return new Promise((res) => { settleCancel = res })
    return Promise.resolve({})
  })
  const tick = () => new Promise((r) => setTimeout(r, 0))
  return {
    runtime,
    phase: composer.turn.phase,
    pinged,
    frame: (event: unknown) => { act(() => { pipeline.stream({ subscription_id: 'sub-1', event }) }) },
    settle: async (reply: CancelReply) => { act(() => { settleCancel!(reply) }); await tick(); await tick() },
    answers: () => [...document.querySelectorAll('.answer')].map((n) => n.textContent),
  }
}

/* The server had already finished the turn when the cancel reached it, so the
   turn's own closing frames arrive while the page is cancelling, and the
   message.complete among them is dropped. A turn that ended on the model's
   silence marker closes with a notice as well, and nothing of that turn may
   decide how the next one lands. */
describe('a stop that crosses the end of a turn', () => {
  const START = (id: string, content: string) => ({ type: 'message.start', payload: { turn_id: id, content } })
  const EPISODE = { type: 'episode.start', payload: { index: 0 } }
  const DONE = (id: string) => ({ type: 'message.complete', payload: { turn_id: id, usage: {}, duration_ms: 2000 } })
  const SILENT = { type: 'notice', payload: { kind: 'silent_turn', detail: '', transient: false } }

  it.each<[string, unknown[]]>([
    ['ended silent', [SILENT]],
    ['ended with no notice (the control)', []],
  ])('leaves the next turn its answer and its ping when the stopped one %s', async (_how, closing) => {
    const h = await page()
    h.frame(START('t1', 'first question'))
    h.frame(EPISODE)
    act(() => { h.runtime.stop() })
    expect(h.phase()).toBe('cancelling')

    closing.forEach((ev) => h.frame(ev))
    h.frame(DONE('t1'))
    await h.settle({ cancelled: false })

    /* A turn this page did not send: another window's, on the same conversation. */
    h.frame(START('t2', 'asked from the other window'))
    h.frame(EPISODE)
    h.frame({ type: 'token.delta', payload: { text: 'the next answer' } })
    h.frame(DONE('t2'))

    expect(h.answers()).toEqual(['the next answer'])
    expect(h.pinged).toEqual([['gui.set.ntf.done', 'a chat']])
  })

  /* The refusal can also land first and say the lane is still busy: the reply
     does not wait out the server's coalescing window and the frames do. The
     stopped turn's closing frames then arrive while the page holds the lane,
     not while it is cancelling, and its completion is dropped because the stop
     already folded that turn. When the turn the lane was busy with is a cron
     wake, which sends no opening frame, no opener comes to clear the mark. */
  it.each<[string, unknown[]]>([
    ['ended silent', [SILENT]],
    ['ended with no notice (the control)', []],
  ])('leaves a wake that opens no turn its answer and its ping when a held stop %s', async (_how, closing) => {
    const h = await page()
    h.frame(START('t1', 'first question'))
    h.frame(EPISODE)
    act(() => { h.runtime.stop() })
    await h.settle({ cancelled: false, busy: true })
    expect(h.phase()).toBe('streaming')

    closing.forEach((ev) => h.frame(ev))
    h.frame(DONE('t1'))

    h.frame(EPISODE)
    h.frame({ type: 'token.delta', payload: { text: 'the wake answer' } })
    h.frame(DONE('t-wake'))

    expect(h.answers()).toEqual(['the wake answer'])
    expect(h.pinged).toEqual([['gui.set.ntf.done', 'a chat']])
  })
})
