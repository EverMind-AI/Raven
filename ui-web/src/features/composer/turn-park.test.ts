// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'

import { loadPart, looseQuery } from '../../../scripts/legacy-part.mjs'

import * as turn from './turn'

type ParkedPart = typeof import('../../legacy/live/060-parked.js')

async function harness(): Promise<{
  part: ParkedPart
  queueRestore: ReturnType<typeof vi.fn>
  workspaceRestore: ReturnType<typeof vi.fn>
  drainQueue: ReturnType<typeof vi.fn>
  setOwner(owner: string): void
  setCurrent(value: string): void
}> {
  document.body.innerHTML = '<div id="stage"><div id="answer">partial</div></div>'
  let current = 'a'
  const queueRestore = vi.fn()
  const drainQueue = vi.fn()
  const workspace = { changes: [], urls: [], file: null, turn: 1, unseen: 0 }
  const workspaceRestore = vi.fn((next: typeof workspace) => Object.assign(workspace, next))
  const part = (await loadPart(() => import('../../legacy/live/060-parked.js'), {
    fakes: {
      'demo/010-kernel.js': { $: looseQuery() },
      'demo/040-state.js': {
        down: vi.fn(),
        queueRestore,
        queueSnapshot: () => ['queued'],
        sess: () => ({ status: null }),
        /* The island's own phase machine, which is what is under test here. */
        turn,
      },
      'demo/050-rail.js': { sessionDraw: vi.fn() },
      'demo/090-composer.js': { drawMeter: vi.fn(), goState: vi.fn() },
      'demo/100-workspace.js': {
        drawWs: vi.fn(),
        wsOpen: false,
        wsRestore: vi.fn(),
        wsView: () => ({ tab: 'files', picked: null }),
      },
      'live/050-turn.js': {
        live: {
          st: null, steps: [], say: '', open: new Map(), sawEpisode: false,
          startedAt: 1, answerAt: 0,
        },
        onEvent: vi.fn(),
        paintSay: vi.fn(),
        stopSayPaint: vi.fn(),
      },
      'live/080-overrides.js': { drainQueue },
    },
    globals: {
      RavenIslands: {
        composer: { liveAnchor: () => 42, setLiveAnchor: vi.fn() },
        workspace: { snapshot: () => ({ ...workspace }), restore: workspaceRestore },
      },
      sessionCurrent: () => current,
      drawBanner: vi.fn(),
    },
  })) as ParkedPart
  return {
    part,
    queueRestore,
    workspaceRestore,
    drainQueue,
    /* `park.turnOwner` is inferred null from its initialiser; the part
       writes a session key into it. */
    setOwner: (owner) => { (part.park as { turnOwner: string | null }).turnOwner = owner },
    setCurrent: (value) => { current = value },
  }
}

afterEach(() => turn._resetForTests())

describe('the legacy parked-turn adapter', () => {
  it('round-trips the phase and queue through island accessors', async () => {
    const { part, queueRestore, workspaceRestore, setOwner } = await harness()
    const { parkTurn, restoreTurn, parkedTurns } = part
    setOwner('a')
    /* `turn.busy()` is what parkTurn gates on, so the phase has to be real. */
    turn.dispatch({ type: 'stream', cancellable: false })
    parkTurn()
    const parked = parkedTurns.get('a')!
    expect(parked.phase).toEqual({ phase: 'streaming', cancellable: false, resume: null })
    expect(parked.queue).toEqual(['queued'])
    expect(parked.ws).toEqual({ changes: [], urls: [], file: null, turn: 1, unseen: 0 })

    turn.dispatch({ type: 'idle' })
    restoreTurn(parked)
    expect(turn.snapshot()).toEqual({ phase: 'streaming', cancellable: false, resume: null })
    expect(queueRestore).toHaveBeenCalledWith(['queued'])
    expect(workspaceRestore).toHaveBeenCalledWith(parked.ws)
    expect(document.getElementById('answer')?.textContent).toBe('partial')
  })

  it('updates a parked conversation without changing the visible phase', async () => {
    const { part, drainQueue, setCurrent, setOwner } = await harness()
    const { parkTurn, restoreTurn, transitionTurn, parkedTurns } = part
    setOwner('a')
    turn.dispatch({ type: 'stream', cancellable: true })
    parkTurn()
    setCurrent('b')
    turn.dispatch({ type: 'idle' })

    transitionTurn('a', { type: 'wait' })
    expect(turn.phase()).toBe('idle')
    expect(parkedTurns.get('a')?.phase.phase).toBe('waiting')
    transitionTurn('a', { type: 'resume' })
    expect(parkedTurns.get('a')?.phase.phase).toBe('streaming')
    transitionTurn('a', { type: 'idle' })

    setCurrent('a')
    restoreTurn(parkedTurns.get('a'))
    expect(turn.phase()).toBe('idle')
    expect(drainQueue).toHaveBeenCalledOnce()
  })
})
