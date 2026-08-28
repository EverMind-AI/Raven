/* The assembled live layer draws the runtime's turn clock, not its own. */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const build = readFileSync(new URL('../build.py', import.meta.url), 'utf8')
const manifest = build.match(/_LIVE_PARTS = \[(.*?)\n\]/s)
if (!manifest) throw new Error('_LIVE_PARTS is absent from build.py')
const parts = [...manifest[1].matchAll(/"([^"]+\.js)"/g)].map((m) => m[1])
const live = parts
  .map((name) => readFileSync(new URL(`../src/live/${name}`, import.meta.url), 'utf8'))
  .join('')

/* The clock, lifted out of the assembled layer and run against a `live` we
   control -- the point is which of the two numbers it returns, so both have to
   be observable and different. */
function turnClock(state) {
  const mark = 'const turnDur = ('
  const start = live.indexOf(mark)
  if (start < 0) throw new Error('turnDur is absent from the assembled live layer')
  const end = live.indexOf('\n};', start)
  if (end < 0) throw new Error('turnDur has no closing brace in the assembled live layer')
  const src = live.slice(start, end + 3)
  return Function('live', 'dur', `${src}\nreturn turnDur;`)(state, (ms) => `${ms}ms`)
}

describe('the assembled live turn clock', () => {
  /* A delegated turn is the case that separates them: the runtime ran it for
     20s, while this page's stamps span the sub-agent's whole absence. */
  const state = { startedAt: 1_000_000, answerAt: 1_268_000 }

  it('draws the duration the runtime measured, not the one this page timed', () => {
    expect(turnClock(state)(20_440)).toBe('20440ms')
  })

  it('falls back to its own stamps when the server sends no duration', () => {
    expect(turnClock(state)(undefined)).toBe('268000ms')
  })

  it('keeps the one-second floor on the runtime number', () => {
    expect(turnClock(state)(12)).toBe('1000ms')
  })

  it('reports nothing when it has neither a server number nor an anchor', () => {
    expect(turnClock({ startedAt: 0, answerAt: 0 })(undefined)).toBe(null)
  })
})

/* The real `finishTurn` from the assembled layer, run against stubs -- the
   claim is that the duration reaches the clock, and only running it can show
   that. Its `turnDur` is the assembled one too, so the two halves are wired
   here exactly as they are on the page. */
function assembledFinishTurn(state, stubs) {
  const mark = 'function finishTurn(p) {'
  const start = live.indexOf(mark)
  if (start < 0) throw new Error('finishTurn is absent from the assembled live layer')
  const end = live.indexOf('\n}', start)
  if (end < 0) throw new Error('finishTurn has no closing brace in the assembled live layer')
  const turnDurMark = 'const turnDur = ('
  const tStart = live.indexOf(turnDurMark)
  const tEnd = live.indexOf('\n};', tStart)
  const src = `${live.slice(tStart, tEnd + 3)}\n${live.slice(start, end + 2)}`
  const names = Object.keys(stubs)
  return Function('live', 'dur', ...names, `${src}\nreturn finishTurn;`)(
    state, (ms) => `${ms}ms`, ...names.map((n) => stubs[n]),
  )
}

describe('the assembled live turn wiring', () => {
  it('hands the clock the duration off message.complete, not its own stamps', () => {
    const drawn = []
    const noop = () => {}
    const stubs = {
      killStatus: noop,
      RavenIslands: {
        transcript: { finishTurn: (_st, _steps, clock) => drawn.push(clock), artifacts: noop },
        workspace: { currentTurn: () => 1 },
      },
      turn: { dispatch: noop },
      setCtx: noop,
      sess: () => null,
      sessionCurrent: () => 's1',
      touchSession: noop,
      ntfPush: noop,
      T: (k) => k,
      resetTurnState: noop,
      drawMeter: noop,
      goState: noop,
      sessionDraw: noop,
      down: noop,
      queueShift: () => undefined,
      liveSend: noop,
    }
    /* Stamps that span four and a half minutes -- a delegated turn's wait --
       against a runtime that says the turn itself took twenty seconds. */
    const state = { st: null, steps: [], say: '', startedAt: 1_000_000, answerAt: 1_268_000 }
    assembledFinishTurn(state, stubs)({ usage: {}, duration_ms: 20_440 })
    expect(drawn).toEqual(['20440ms'])
  })

  it('still draws a clock when the server sends no duration', () => {
    const drawn = []
    const noop = () => {}
    const stubs = {
      killStatus: noop,
      RavenIslands: {
        transcript: { finishTurn: (_st, _steps, clock) => drawn.push(clock), artifacts: noop },
        workspace: { currentTurn: () => 1 },
      },
      turn: { dispatch: noop },
      setCtx: noop,
      sess: () => null,
      sessionCurrent: () => 's1',
      touchSession: noop,
      ntfPush: noop,
      T: (k) => k,
      resetTurnState: noop,
      drawMeter: noop,
      goState: noop,
      sessionDraw: noop,
      down: noop,
      queueShift: () => undefined,
      liveSend: noop,
    }
    const state = { st: null, steps: [], say: '', startedAt: 1_000_000, answerAt: 1_268_000 }
    assembledFinishTurn(state, stubs)({ usage: {} })
    expect(drawn).toEqual(['268000ms'])
  })

  it('re-anchors the fallback clock on a turn nobody typed', () => {
    const mark = "} else if (ev.type === 'turn.started') {"
    const start = live.indexOf(mark)
    expect(start).toBeGreaterThan(-1)
    const next = live.indexOf("} else if (ev.type === 'episode.start') {", start)
    expect(next).toBeGreaterThan(start)
    expect(live.slice(start, next)).toContain('live.startedAt = Date.now();')
  })
})
