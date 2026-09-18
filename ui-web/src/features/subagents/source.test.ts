// @vitest-environment happy-dom
/* The heartbeat behind a delegated run that is still moving.
 *
 * A run in flight has to move on screen without being reopened, and there is
 * no push for it: the source polls and forwards, and every judgement about what
 * that takes belongs to the island that is drawing it. The interval is the
 * half that can be dropped silently -- nothing else fails when a panel merely
 * stops refreshing.
 *
 * Opened by the page's wiring, beside the handlers for the pushes that do
 * exist (src/app/install.ts), so that is what this drives.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { fakeGateway, loadPart } from '../../../scripts/module-harness.mjs'

import type { Sources } from '../../state/sources'

async function harness() {
  const wiring = await loadPart(async () => {
    await import('./source')
    return import('../../app/install')
  }, {
    fakes: {
      'src/lib/session': { current: () => 's1' },
      'src/state/session/runtime': { mediaOf: () => ({}) },
      'src/features/transcript/mount': {
        agentStage: () => {},
      },
      'src/features/subagents/store': {
        directEvent: () => {},
      },
    },
  })
  await fakeGateway(() => Promise.resolve({}))
  const { setSources, sources } = await import('../../state/sources')
  setSources({ composer: { slash: [] }, rail: {}, transcript: {} } as unknown as Partial<Sources>)
  wiring.installSources()
  /* The interval is opened with the push handlers, so the clock has to be fake
     before they install. */
  vi.useFakeTimers()
  wiring.installPushes()
  return { sources }
}

afterEach(() => vi.useRealTimers())

describe('the sub-agent heartbeat', () => {
  it('calls the registered watcher every two seconds', async () => {
    const { sources } = await harness()
    const beat = vi.fn()

    sources.subagents!.watch!(beat)
    await vi.advanceTimersByTimeAsync(2000)
    expect(beat).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(4000)
    expect(beat).toHaveBeenCalledTimes(3)
  })

  it('beats against nothing until an island registers, and keeps no backlog', async () => {
    /* The interval runs for the life of the tab whether or not the panel is
       open, so the absent watcher is the ordinary case, not an error. */
    const { sources } = await harness()

    await vi.advanceTimersByTimeAsync(6000)
    const beat = vi.fn()
    sources.subagents!.watch!(beat)
    await vi.advanceTimersByTimeAsync(2000)

    expect(beat).toHaveBeenCalledTimes(1)
  })
})
