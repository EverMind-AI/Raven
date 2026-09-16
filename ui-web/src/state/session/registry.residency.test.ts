// @vitest-environment happy-dom
/* What a conversation keeps while it is off screen.
 *
 * Server-side subscriptions are additive and never torn down, so a background
 * conversation's turn keeps streaming over the socket. Leaving it mid-turn
 * files the turn under the conversation it belongs to and buffers the frames
 * that arrive while it is away; coming back puts both back.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { fakeGateway, loadPart, looseQuery } from '../../../scripts/legacy-part.mjs'

import * as turn from '../../features/composer/turn'
import type { Sources } from '../sources'

type Registry = typeof import('./registry')
type SessionRuntime = import('./runtime').SessionRuntime

/* What a conversation is holding, which is the runtime itself. */
interface Parked {
  phase: unknown
  queue: unknown
  ws: unknown
  events: unknown[]
}

/* The map the parked turns used to live in, over the conversations that hold
   them now. */
interface ParkedPart {
  parkedTurns: { get(key: string): Parked | undefined; has(key: string): boolean }
}

async function harness(): Promise<{
  registry: Registry
  part: ParkedPart
  queueRestore: ReturnType<typeof vi.fn>
  workspaceRestore: ReturnType<typeof vi.fn>
  drainQueue: ReturnType<typeof vi.fn>
  log: string[]
  rows: Array<{ id: string; status: string | null }>
  setOwner(owner: string): void
  setCurrent(value: string): void
}> {
  document.body.innerHTML = '<div id="stage"><div id="answer">partial</div></div>'
  let current = 'a'
  const log: string[] = []
  const rows = [{ id: 'a', status: null as string | null }, { id: 'b', status: null as string | null }]
  const queueRestore = vi.fn()
  const drainQueue = vi.fn()
  const workspace = { changes: [], urls: [], file: null, turn: 1, unseen: 0 }
  const workspaceRestore = vi.fn((next: typeof workspace) => Object.assign(workspace, next))
  /* The modules under test are imported deepest first, which is the order that
     keeps one module graph: the fakes are installed around the modules the
     first import reaches, and one it did not is loaded afterwards without
     them. */
  await loadPart(() => import('./registry'), {
    fakes: {
      'src/shell/session': { current: () => current },
      'src/shell/banner': { draw: vi.fn() },
      'demo/010-kernel.js': { $: looseQuery() },
      'demo/040-state.js': {
        down: vi.fn(),
        queueRestore,
        queueSnapshot: () => ['queued'],
        sess: (id: string) => rows.find((r) => r.id === id),
        /* The island's own phase machine, which is what is under test here. */
        turn,
      },
      'demo/050-rail.js': { sessionDraw: vi.fn() },
      'demo/090-composer.js': { drawMeter: () => log.push('drawMeter'), goState: vi.fn() },
      'demo/100-workspace.js': {
        drawWs: vi.fn(),
        wsOpen: false,
        wsRestore: vi.fn(),
        wsView: () => ({ tab: 'files', picked: null }),
      },
      /* What a buffered frame means, which is the pipeline's and not the
         residency rule's. */
      'src/state/session/runtime': { drain: drainQueue },
      'src/state/session/stages': {
        dispatch: (ev: { type?: string }) => {
          log.push(`onEvent:${ev && ev.type}`)
          if (ev && ev.type === 'bad') throw new Error('a bad frame')
        },
      },
    },
    islands: {
      composer: { liveAnchor: () => 42, setLiveAnchor: () => log.push('setLiveAnchor') },
      workspace: { snapshot: () => ({ ...workspace }), restore: workspaceRestore },
      transcript: { nudge: vi.fn(), stopStream: vi.fn() },
    },
  })
  const registry = (await import('./registry')) as Registry
  const part: ParkedPart = {
    parkedTurns: {
      get: (key) => registry.parked(key) as unknown as Parked | undefined,
      has: (key) => registry.isResident(key),
    },
  }
  return {
    registry,
    part,
    queueRestore,
    workspaceRestore,
    drainQueue,
    log,
    rows,
    /* Which conversation the page is showing, which is what a leave files the
       turn under -- never the session pointer, which a rail click has already
       moved to the conversation being opened. */
    setOwner: (owner) => { registry.adopt(owner) },
    setCurrent: (value) => { current = value },
  }
}

afterEach(() => turn._resetForTests())

describe('the legacy parked-turn adapter', () => {
  it('round-trips the phase and queue through island accessors', async () => {
    const { registry, part, queueRestore, workspaceRestore, setOwner } = await harness()
    const { parkedTurns } = part
    setOwner('a')
    /* `turn.busy()` is what parkTurn gates on, so the phase has to be real. */
    turn.dispatch({ type: 'stream', cancellable: false })
    registry.park()
    const parked = parkedTurns.get('a')! as Parked
    expect(parked.phase).toEqual({ phase: 'streaming', cancellable: false, resume: null })
    expect(parked.queue).toEqual(['queued'])
    expect(parked.ws).toEqual({ changes: [], urls: [], file: null, turn: 1, unseen: 0 })

    turn.dispatch({ type: 'idle' })
    registry.resume(parked as unknown as SessionRuntime)
    expect(turn.snapshot()).toEqual({ phase: 'streaming', cancellable: false, resume: null })
    expect(queueRestore).toHaveBeenCalledWith(['queued'])
    expect(workspaceRestore).toHaveBeenCalledWith(parked.ws)
    expect(document.getElementById('answer')?.textContent).toBe('partial')
  })

  it('updates a parked conversation without changing the visible phase', async () => {
    const { registry, part, drainQueue, setCurrent, setOwner } = await harness()
    const { parkedTurns } = part
    setOwner('a')
    turn.dispatch({ type: 'stream', cancellable: true })
    registry.park()
    setCurrent('b')
    turn.dispatch({ type: 'idle' })

    registry.dispatchTo('a', { type: 'wait' })
    expect(turn.phase()).toBe('idle')
    expect((parkedTurns.get('a') as { phase: { phase: string } } | undefined)?.phase.phase).toBe('waiting')
    registry.dispatchTo('a', { type: 'resume' })
    expect((parkedTurns.get('a') as { phase: { phase: string } } | undefined)?.phase.phase).toBe('streaming')
    registry.dispatchTo('a', { type: 'idle' })

    setCurrent('a')
    registry.resume(parkedTurns.get('a') as unknown as SessionRuntime)
    expect(turn.phase()).toBe('idle')
    expect(drainQueue).toHaveBeenCalledOnce()
  })
})

/* N10: which drawer the turn goes in. Every rail click moves the session
   pointer BEFORE the switch runs, so by the time the old turn is parked the
   pointer already names the conversation being opened. */
describe('the drawer a leaving turn is filed under', () => {
  it('files the turn under its owner, not under the pointer', async () => {
    const { registry, setOwner, setCurrent, rows } = await harness()
    setOwner('a')
    turn.dispatch({ type: 'stream', cancellable: true })
    /* The click has already moved the pointer to B. */
    setCurrent('b')

    registry.park()

    expect(registry.isResident('a')).toBe(true)
    expect(registry.isResident('b')).toBe(false)
    /* And the owner's row says a turn is running on it. */
    expect(rows[0]!.status).toBe('run')
    expect(rows[1]!.status).toBeNull()
  })

  it('files nothing at all when no turn is running', async () => {
    /* Not busy is nothing to keep: the conversation is re-read from disk on the
       way back in, which is where the eviction rule comes from. */
    const { registry, setOwner } = await harness()
    setOwner('a')

    registry.park()

    expect(registry.isResident('a')).toBe(false)
    expect(registry.parked('a')).toBeUndefined()
  })
})

/* N11: the order inside the restore, which is the half a reader cannot see. */
describe('coming back to a parked turn', () => {
  it('sets the live clock anchor before the meter is drawn', async () => {
    /* The turn-live paint keeps a non-zero anchor, so the clock resumes from
       the turn's real start rather than from the switch -- a ten-minute turn
       read "2s" after a round trip through another conversation. */
    const { registry, part, setOwner, log } = await harness()
    setOwner('a')
    turn.dispatch({ type: 'stream', cancellable: true })
    registry.park()
    log.length = 0

    registry.resume(part.parkedTurns.get('a') as unknown as SessionRuntime)

    expect(log.indexOf('setLiveAnchor')).toBeGreaterThan(-1)
    expect(log.indexOf('setLiveAnchor')).toBeLessThan(log.indexOf('drawMeter'))
  })

  it('replays every buffered frame, including the ones after a bad one', async () => {
    /* Each frame is replayed in its own try: one frame that throws must not eat
       the rest of the buffer. */
    const { registry, part, setOwner, log } = await harness()
    setOwner('a')
    turn.dispatch({ type: 'stream', cancellable: true })
    registry.park()
    const parked = part.parkedTurns.get('a')! as Parked
    parked.events.push({ type: 'first' }, { type: 'bad' }, { type: 'last' })
    log.length = 0

    registry.resume(parked as unknown as SessionRuntime)

    expect(log.filter((l) => l.startsWith('onEvent')))
      .toEqual(['onEvent:first', 'onEvent:bad', 'onEvent:last'])
  })
})

/* N12: the third case, which is silence. */
describe('a phase event for a conversation that is neither', () => {
  it('drops it, rather than folding it into whatever is open', async () => {
    const { registry, part, setOwner, setCurrent } = await harness()
    setOwner('a')
    turn.dispatch({ type: 'stream', cancellable: true })
    setCurrent('b')

    /* C is not the visible owner and kept no turn: a cancel reply or a request
       arriving for it has nowhere to land. */
    registry.dispatchTo('c', { type: 'wait' })

    expect(turn.phase()).toBe('streaming')
    expect(part.parkedTurns.has('c')).toBe(false)
    expect(registry.isResident('c')).toBe(false)
  })
})

/* Not an N item: the other half of the registry's subscription book, which
   nothing drove before. */
describe('forgetting a conversation subscription', () => {
  it('drops both books, releases the visible stream and unsubscribes', async () => {
    const asked: Array<[string, unknown]> = []
    await loadPart(async () => { await import('./runtime'); return import('./registry') }, {
      fakes: {
        'src/shell/session': { current: () => 'a' },
        'demo/010-kernel.js': { $: looseQuery(), T: (key: string) => key },
      },
    })
    const registry = (await import('./registry')) as Registry
    await fakeGateway((method: string, params: unknown) => {
      asked.push([method, params])
      return Promise.resolve({})
    })
    const { setSources } = await import('../sources')
    setSources({ composer: {}, sessions: {}, transcript: {} } as unknown as Partial<Sources>)
    /* The two books the subscription used to be kept in, and the one field that
       said which stream painted the stage: all three are the conversation's own
       `subscriptionId` now, so they are read back off it. */
    const sub = () => registry.get('a')?.subscriptionId ?? null
    const parked = {
      get subBySession() { return sub() ? { a: sub() } : {} },
      get subSession() { return sub() ? { [sub() as string]: 'a' } : {} },
    }
    const turnState = { live: { get subId() { return sub() } } }
    registry.record('a', 'sub:a')

    registry.forget('a')

    expect(parked.subBySession).toEqual({})
    expect(parked.subSession).toEqual({})
    expect((turnState.live as { subId: string | null }).subId).toBeNull()
    expect(asked).toEqual([['turn.unsubscribe', { subscription_id: 'sub:a' }]])
  })

  it('does nothing for a conversation that never had one', async () => {
    const asked: Array<[string, unknown]> = []
    await loadPart(async () => { await import('./runtime'); return import('./registry') }, {
      fakes: {
        'src/shell/session': { current: () => 'a' },
        'demo/010-kernel.js': { $: looseQuery(), T: (key: string) => key },
      },
    })
    const registry = (await import('./registry')) as Registry
    await fakeGateway((method: string, params: unknown) => {
      asked.push([method, params])
      return Promise.resolve({})
    })

    registry.forget('nobody')

    expect(asked).toEqual([])
  })
})
