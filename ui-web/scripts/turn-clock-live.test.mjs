// @vitest-environment happy-dom
/* The live layer draws the runtime's turn clock, not its own. */

import { readFileSync } from 'node:fs'
/* Off cwd, not off `import.meta.url`: under happy-dom that is an http URL. */
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { loadPart, looseQuery } from './legacy-part.mjs'

/* The clock run against a `live` we control -- the point is which of the two
   numbers it returns, so both have to be observable and different. */
async function turnPart(stamps, stubs = {}) {
  const part = await loadPart(() => import('../src/legacy/live/050-turn.js'), {
    fakes: {
      'demo/010-kernel.js': { $: looseQuery(), dur: (ms) => `${ms}ms`, T: (k) => k },
      'demo/040-state.js': {
        down: () => {},
        queueShift: () => undefined,
        sess: () => null,
        turn: { dispatch: () => {} },
        ...stubs.state,
      },
      'demo/050-rail.js': { sessionDraw: () => {} },
      'demo/070-transcript.js': { killStatus: () => {} },
      'demo/090-composer.js': { drawMeter: () => {}, goState: () => {} },
      'live/030-sessions.js': { touchSession: () => {} },
      'live/080-overrides.js': { liveSend: () => {} },
    },
    globals: {
      RavenIslands: {
        transcript: { nudge: () => {}, stopStream: () => {}, ...stubs.transcript },
        workspace: { currentTurn: () => 1 },
      },
      sessionCurrent: () => 's1',
      setCtx: () => {},
      ntfPush: () => {},
    },
  })
  Object.assign(part.live, stamps)
  return part
}

describe('the live turn clock', () => {
  /* A delegated turn is the case that separates them: the runtime ran it for
     20s, while this page's stamps span the sub-agent's whole absence. */
  const stamps = { startedAt: 1_000_000, answerAt: 1_268_000 }

  it('draws the duration the runtime measured, not the one this page timed', async () => {
    expect((await turnPart(stamps)).turnDur(20_440)).toBe('20440ms')
  })

  it('falls back to its own stamps when the server sends no duration', async () => {
    expect((await turnPart(stamps)).turnDur(undefined)).toBe('268000ms')
  })

  it('keeps the one-second floor on the runtime number', async () => {
    expect((await turnPart(stamps)).turnDur(12)).toBe('1000ms')
  })

  it('reports nothing when it has neither a server number nor an anchor', async () => {
    expect((await turnPart({ startedAt: 0, answerAt: 0 })).turnDur(undefined)).toBe(null)
  })
})

describe('the live turn wiring', () => {
  /* The real `finishTurn` against stubs -- the claim is that the duration
     reaches the clock, and only running it can show that. Its `turnDur` is the
     part's own, so the two halves are wired here exactly as they are on the
     page. */
  const stamps = { st: null, steps: [], say: '', startedAt: 1_000_000, answerAt: 1_268_000 }

  async function finisher(drawn) {
    const part = await turnPart(stamps, {
      transcript: {
        finishTurn: (_st, _steps, clock) => drawn.push(clock),
        artifacts: () => {},
      },
    })
    return part.finishTurn
  }

  it('hands the clock the duration off message.complete, not its own stamps', async () => {
    const drawn = []
    /* Stamps that span four and a half minutes -- a delegated turn's wait --
       against a runtime that says the turn itself took twenty seconds. */
    ;(await finisher(drawn))({ usage: {}, duration_ms: 20_440 })
    expect(drawn).toEqual(['20440ms'])
  })

  it('still draws a clock when the server sends no duration', async () => {
    const drawn = []
    ;(await finisher(drawn))({ usage: {} })
    expect(drawn).toEqual(['268000ms'])
  })

  it('re-anchors the fallback clock on a turn nobody typed', () => {
    /* An ordering rule about one branch of the dispatcher, so it is read off
       the part's own text: the branch runs for an event the harness above
       cannot distinguish from the one before it. */
    const src = readFileSync(resolve(process.cwd(), 'src/legacy/live/050-turn.js'), 'utf8')
    const mark = "} else if (ev.type === 'turn.started') {"
    const start = src.indexOf(mark)
    expect(start).toBeGreaterThan(-1)
    const next = src.indexOf("} else if (ev.type === 'episode.start') {", start)
    expect(next).toBeGreaterThan(start)
    expect(src.slice(start, next)).toContain('live.startedAt = Date.now();')
  })
})
