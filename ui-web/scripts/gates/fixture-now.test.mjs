// @vitest-environment happy-dom
/* The offline library has no clock of its own.
 *
 * Every time field a fixture answers comes from the `now()` handed to the
 * transport, so the same library built twice on the same instant answers
 * byte-identically. A residual `Date.now()` is invisible in a unit test -- it
 * only shows up as an offline page whose stamps move between two runs, and as a
 * boot snapshot that cannot be pinned -- so this is where it is caught.
 *
 * Two libraries rather than two passes over one: the responders hold state on
 * purpose (a toggle, a save and a delete all change what the next list
 * answers), so replaying a call into the same library is meant to differ. What
 * must not differ is the same first call made against two libraries born at the
 * same instant.
 */

import { describe, expect, it } from 'vitest'

import { PARAMS } from '../fixture-params.mjs'

/* Not "now": a fixed instant with a known local wording, so a failure reads as
   a drifting field rather than as a clock that moved. */
const FIXED = 1789000000000

async function library() {
  const { demoFixtures } = await import('../../src/rpc/fixtures/index.ts')
  const { FixtureTransport } = await import('../../src/rpc/fixtureTransport.ts')
  /* Scheduled work is collected rather than run: a scripted turn's frames are
     pushed on this timer, and a gate that ran them would be asserting on the
     page instead of on the answers. */
  const queued = []
  const transport = new FixtureTransport(demoFixtures, {
    now: () => FIXED,
    timer: (ms, fn) => { queued.push([ms, fn]) },
  })
  return { transport, queued }
}

/* Every method the library answers, plus the calls whose answer depends on
   which row was asked for -- one list, so the two passes walk it in one order. */
async function answers() {
  const { transport, queued } = await library()
  const out = {}
  for (const method of Object.keys(transport.fixtures)) {
    out[method] = await transport.call(method, PARAMS[method] ?? {})
  }
  for (const id of ['a', 'b', 'g', 'k1']) {
    out[`session.resume(${id})`] = await transport.call('session.resume', { session_id: id })
  }
  /* What a scripted turn would have pushed, in order, with the delay each frame
     was scheduled for: the frames carry stamps too. */
  out['__scheduled'] = queued.map(([ms]) => ms)
  return out
}

describe('the offline fixture library on a fixed clock', () => {
  it('answers byte-identically twice over', async () => {
    const first = await answers()
    const second = await answers()
    for (const key of Object.keys(first)) {
      expect(JSON.stringify(second[key]), key).toBe(JSON.stringify(first[key]))
    }
    expect(Object.keys(second)).toEqual(Object.keys(first))
  })

  /* The one place a stamp is visible as prose: a session row's instant, which
     the rail words itself. Pinned so a fixture that started answering `now()`
     directly -- rather than an offset from it -- is caught by the value and not
     only by the comparison above. */
  it('stamps its session rows off the injected instant', async () => {
    const { transport } = await library()
    const listed = await transport.call('session.list', { channels: ['tui', 'cron'] })
    const updated = listed.sessions.map((s) => s.updated_at)
    expect(updated.every((at) => at * 1000 <= FIXED)).toBe(true)
    expect(Math.max(...updated) * 1000).toBe(FIXED - 2 * 3600000)
  })
})
