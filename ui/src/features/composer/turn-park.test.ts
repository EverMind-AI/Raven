// @vitest-environment happy-dom
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'

import { afterEach, describe, expect, it, vi } from 'vitest'

import * as turn from './turn'

interface ParkedHarness {
  parkTurn(): void
  restoreTurn(value: unknown): void
  transitionTurn(owner: string, event: turn.TurnEvent): void
  parkedTurns: Map<string, { phase: turn.TurnSnapshot; queue: string[]; ws: unknown }>
  setOwner(owner: string): void
}

const source = readFileSync('src/live/060-parked.js', 'utf8')

function harness(): {
  api: ParkedHarness
  queueRestore: ReturnType<typeof vi.fn>
  workspaceRestore: ReturnType<typeof vi.fn>
  drainQueue: ReturnType<typeof vi.fn>
  setCurrent(value: string): void
} {
  document.body.innerHTML = '<div id="stage"><div id="answer">partial</div></div>'
  let current = 'a'
  const queueRestore = vi.fn()
  const drainQueue = vi.fn()
  const workspace = { changes: [], urls: [], file: null, turn: 1, unseen: 0 }
  const workspaceRestore = vi.fn((next: typeof workspace) => Object.assign(workspace, next))
  const raven = {
    composer: {
      liveAnchor: () => 42,
      setLiveAnchor: vi.fn(),
    },
    workspace: {
      snapshot: () => ({ ...workspace }),
      restore: workspaceRestore,
    },
  }
  const names = [
    'DS', 'turn', 'stopSayPaint', 'sess', '$', 'live', 'queueSnapshot', 'RavenIslands', 'wsView',
    'sessionCurrent', 'queueRestore', 'wsRestore', 'onEvent', 'paintSay', 'drawMeter', 'goState',
    'sessionDraw', 'drawBanner', 'drawWs', 'wsOpen', 'down', 'drainQueue',
  ]
  const values = [
    { transcript: {} }, turn, vi.fn(), () => ({ status: null }),
    (selector: string) => document.querySelector(selector),
    { st: null, steps: [], say: '', open: new Map(), sawEpisode: false, startedAt: 1, answerAt: 0 },
    () => ['queued'], raven,
    () => ({ tab: 'files', picked: null }), () => current, queueRestore, vi.fn(), vi.fn(), vi.fn(), vi.fn(),
    vi.fn(), vi.fn(), vi.fn(), undefined, false, vi.fn(), drainQueue,
  ]
  const build = new Function(...names, `${source}\nreturn {
    parkTurn, restoreTurn, transitionTurn, parkedTurns,
    setOwner(value) { turnOwner = value; },
  };`) as (...args: unknown[]) => ParkedHarness
  return {
    api: build(...values), queueRestore, workspaceRestore, drainQueue,
    setCurrent: (value) => { current = value },
  }
}

afterEach(() => turn._resetForTests())

describe('the legacy parked-turn adapter', () => {
  it('round-trips the phase and queue through island accessors', () => {
    const { api, queueRestore, workspaceRestore } = harness()
    api.setOwner('a')
    turn.dispatch({ type: 'stream', cancellable: false })
    api.parkTurn()
    const parked = api.parkedTurns.get('a')!
    expect(parked.phase).toEqual({ phase: 'streaming', cancellable: false, resume: null })
    expect(parked.queue).toEqual(['queued'])
    expect(parked.ws).toEqual({ changes: [], urls: [], file: null, turn: 1, unseen: 0 })

    turn.dispatch({ type: 'idle' })
    api.restoreTurn(parked)
    expect(turn.snapshot()).toEqual({ phase: 'streaming', cancellable: false, resume: null })
    expect(queueRestore).toHaveBeenCalledWith(['queued'])
    expect(workspaceRestore).toHaveBeenCalledWith(parked.ws)
    expect(document.getElementById('answer')?.textContent).toBe('partial')
  })

  it('updates a parked conversation without changing the visible phase', () => {
    const { api, drainQueue, setCurrent } = harness()
    api.setOwner('a')
    turn.dispatch({ type: 'stream', cancellable: true })
    api.parkTurn()
    setCurrent('b')
    turn.dispatch({ type: 'idle' })

    api.transitionTurn('a', { type: 'wait' })
    expect(turn.phase()).toBe('idle')
    expect(api.parkedTurns.get('a')?.phase.phase).toBe('waiting')
    api.transitionTurn('a', { type: 'resume' })
    expect(api.parkedTurns.get('a')?.phase.phase).toBe('streaming')
    api.transitionTurn('a', { type: 'idle' })

    setCurrent('a')
    api.restoreTurn(api.parkedTurns.get('a'))
    expect(turn.phase()).toBe('idle')
    expect(drainQueue).toHaveBeenCalledOnce()
  })
})
