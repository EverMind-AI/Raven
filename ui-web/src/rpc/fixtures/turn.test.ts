/* What a scripted turn pushes at the page while it runs.
 *
 * The library's method RESULTS are held to the contract by a gate next door
 * (scripts/gates/fixture-shape.test.mjs), and that covers what a reload reads
 * back. A turn's frames are notifications, which no gate walks -- so the half
 * of the demo a reader actually watches, the rows arriving as a call finishes,
 * is pinned here instead. A field the live frame stops carrying leaves the
 * offline page drawing one thing and the live page another, which is the one
 * divergence this library exists to prevent.
 */

import { describe, expect, it } from 'vitest'

import { FixtureTransport } from '../fixtureTransport'
import { demoFixtures } from './index'
import { RUNS } from './turn'

import type { ToolCompleteEvent } from '../generated'

interface Frame { event?: { type?: string; payload?: Record<string, unknown> } }

/* The script's own clock collapsed onto this one: every frame is delivered as
   it is scheduled, so one send plays the whole conversation. */
async function framesOf(content: string): Promise<Frame[]> {
  const frames: Frame[] = []
  const transport = new FixtureTransport(demoFixtures, { now: () => 1789000000000, timer: (_ms, fn) => fn() })
  transport.on('event', (p) => frames.push(p as Frame))
  await transport.call('turn.subscribe', { session_key: 's1' })
  await transport.call('turn.send', { session_key: 's1', content })
  return frames
}

describe('the frames a scripted turn pushes', () => {
  it('sends the files a command left behind on the call that ran it', async () => {
    /* The script's own opening question rather than one spelled here: which
       of the two conversations plays is decided by the words in it. */
    const frames = await framesOf(RUNS.gtm!.ask)

    const written = frames
      .map((f) => f.event)
      .filter((e) => e?.type === 'tool.complete' && e.payload?.file_written)
      .map((e) => (e!.payload as unknown as ToolCompleteEvent['payload']).file_written)
    expect(written).toEqual([[
      { path: '~/work/raven/research/tally.txt', created: true, size: 96, lines: 4 },
      { path: '~/work/raven/research/run.log', created: false, size: 412, lines: null },
    ]])
  })
})
