// @vitest-environment happy-dom
/* The heartbeat behind a delegated run that is still moving.
 *
 * A run in flight has to move on screen without being reopened, and there is
 * no push for it: the source polls and forwards, and every judgement about what
 * that takes belongs to the island that is drawing it. The interval was
 * untested while it lived in the legacy layer, which is the half a rewrite can
 * silently drop -- nothing else fails when a panel merely stops refreshing.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { loadPart } from '../../../scripts/legacy-part.mjs'

import type { Sources } from '../../state/sources'

async function harness() {
  const part = await loadPart(async () => {
    await import('./source')
    return import('../../legacy/live/230-tabs.js')
  }, {
    fakes: {
      'src/shell/session': { current: () => 's1' },
      'src/state/session/runtime': { mediaOf: () => ({}) },
    },
    islands: { transcript: { agentStage: () => {} }, subagents: { directEvent: () => {} } },
  })
  const { setSources, sources } = await import('../../state/sources')
  setSources({ agents: {} } as unknown as Partial<Sources>)
  /* The interval is opened by install(), so the clock has to be fake before
     it runs. */
  vi.useFakeTimers()
  part.install()
  return { sources }
}

afterEach(() => vi.useRealTimers())

describe('the sub-agent heartbeat', () => {
  it('calls the registered watcher every two seconds', async () => {
    const { sources } = await harness()
    const beat = vi.fn()

    sources.agents!.watch!(beat)
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
    sources.agents!.watch!(beat)
    await vi.advanceTimersByTimeAsync(2000)

    expect(beat).toHaveBeenCalledTimes(1)
  })
})
