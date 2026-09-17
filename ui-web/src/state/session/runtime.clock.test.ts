// @vitest-environment happy-dom
/* The runtime draws the engine's turn clock, not its own. */

import { describe, expect, it } from 'vitest'

import { loadPart, looseQuery, moduleText } from '../../../scripts/module-harness.mjs'

type Runtime = typeof import('./runtime')

/* The clock run against a turn state we control -- the point is which of the
   two numbers it returns, so both have to be observable and different. */
async function turnPart(
  stamps: Record<string, unknown>,
  stubs: { transcript?: Record<string, unknown> } = {},
): Promise<Runtime> {
  /* The module under test is what the fakes are installed around: a module
     the first import did not reach is loaded afterwards without them. */
  await loadPart(() => import('./runtime'), {
    fakes: {
      'src/features/rail/store': { draw: () => {} },
      'src/state/session/rows': { sess: () => null },
      'src/features/composer/mount': {
        queueShift: () => undefined,
        turn: { dispatch: () => {} },
        drawMeter: () => {},
        goPaint: () => {},
      },
      'src/i18n/t': { T: (k: string) => k },
      'src/shell/duration': { formatDuration: (ms: number) => `${ms}ms` },
      'src/shell/dom': { $: looseQuery() },
      'src/shell/session': { current: () => 's1' },
      'src/shell/ctxchip': { set: () => {} },
      'src/shell/notifications': { show: () => {} },
      'src/state/session/registry': { touch: () => {} },
    },
    islands: {
      transcript: {
        nudge: () => {},
        stopStream: () => {},
        ...stubs.transcript,
        down: () => {},
        killStatus: () => {},
      },
      workspace: { currentTurn: () => 1 },
    },
  })
  const runtime = (await import('./runtime')) as Runtime
  Object.assign(runtime.state(), stamps)
  return runtime
}

describe('the live turn clock', () => {
  /* A delegated turn is the case that separates them: the runtime ran it for
     20s, while this page's stamps span the sub-agent's whole absence. */
  const stamps = { startedAt: 1_000_000, answerAt: 1_268_000 }

  it('draws the duration the runtime measured, not the one this page timed', async () => {
    expect((await turnPart(stamps)).duration(20_440)).toBe('20440ms')
  })

  it('falls back to its own stamps when the server sends no duration', async () => {
    expect((await turnPart(stamps)).duration(undefined)).toBe('268000ms')
  })

  it('keeps the one-second floor on the runtime number', async () => {
    expect((await turnPart(stamps)).duration(12)).toBe('1000ms')
  })

  it('reports nothing when it has neither a server number nor an anchor', async () => {
    expect((await turnPart({ startedAt: 0, answerAt: 0 })).duration(undefined)).toBe(null)
  })
})

describe('the live turn wiring', () => {
  /* The real `finishTurn` against stubs -- the claim is that the duration
     reaches the clock, and only running it can show that. Its `duration` is the
     same one, so the two halves are wired here exactly as they are on the
     page. */
  const stamps = { st: null, steps: [], say: '', startedAt: 1_000_000, answerAt: 1_268_000 }

  async function finisher(drawn: Array<string | null>) {
    const runtime = await turnPart(stamps, {
      transcript: {
        finishTurn: (_st: unknown, _steps: unknown, clock: string | null) => drawn.push(clock),
        artifacts: () => {},
      },
    })
    return runtime.finishTurn
  }

  it('hands the clock the duration off message.complete, not its own stamps', async () => {
    const drawn: Array<string | null> = []
    /* Stamps that span four and a half minutes -- a delegated turn's wait --
       against a runtime that says the turn itself took twenty seconds. */
    ;(await finisher(drawn))({ usage: {}, duration_ms: 20_440 })
    expect(drawn).toEqual(['20440ms'])
  })

  it('still draws a clock when the server sends no duration', async () => {
    const drawn: Array<string | null> = []
    ;(await finisher(drawn))({ usage: {} })
    expect(drawn).toEqual(['268000ms'])
  })

  it('re-anchors the fallback clock on a turn nobody typed', () => {
    /* An ordering rule about one stage of the pipeline, so it is read off the
       stage table's own text: the stage runs for an event the harness above
       cannot distinguish from the one before it. */
    const src = moduleText('state/session/stages.ts') as string
    const mark = "arm('turn.started'"
    const start = src.indexOf(mark)
    expect(start).toBeGreaterThan(-1)
    const next = src.indexOf("arm('episode.start'", start)
    expect(next).toBeGreaterThan(start)
    expect(src.slice(start, next)).toContain('rt.startedAt = Date.now()')
  })
})
