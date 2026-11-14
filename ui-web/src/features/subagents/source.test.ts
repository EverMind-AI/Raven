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
import { resetCapabilities } from '../../rpc/capabilities'

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

describe('the instances answer', () => {
  it('hands over the rows and the handoff count the same response carries', async () => {
    /* The count used to be mapped away here, so the page could not draw the
       hint the runtime keeps it for. Both halves come from one call. */
    const { sources } = await harness()
    await fakeGateway(async () => ({
      instances: [{ sessionKey: 's1', agent: 'hermes', handle: 'one', kind: 'cli' }],
      pending_handoff_count: 3,
    }))
    expect(await sources.subagents!.instances!('s1')).toEqual({
      rows: [{ sessionKey: 's1', agent: 'hermes', handle: 'one', kind: 'cli' }],
      pendingHandoff: 3,
    })
  })

  it('answers a gateway error by rethrowing it, keeping the last list drawn', async () => {
    const { sources } = await harness()
    await fakeGateway(async () => { throw new Error('socket dropped') })
    await expect(sources.subagents!.instances!('s1')).rejects.toThrow('socket dropped')
  })

  it('answers an absent surface with no rows and no count', async () => {
    /* A server without the surface refuses the call forever. The panel reads
       that as an empty list rather than as an error -- and the hint has
       nothing to count, so the count comes back zero with it. */
    resetCapabilities()
    const { sources } = await harness()
    await fakeGateway(async () => { throw Object.assign(new Error('no such method'), { code: -32601 }) })
    expect(await sources.subagents!.instances!('s1')).toEqual({ rows: [], pendingHandoff: 0 })
    resetCapabilities()
  })
})

describe('the direct-turn seam', () => {
  it('addresses a stop to the instance lane, carrying the target turn.cancel needs', async () => {
    const { sources } = await harness()
    const calls: Array<[string, Record<string, unknown>]> = []
    await fakeGateway(async (method: string, params: Record<string, unknown>) => {
      calls.push([method, params])
      return {}
    })
    await sources.subagents!.instanceCancel!('Raven-Design', 'handle-1')
    /* Without the target the lookup is the bare session key, and every direct
       turn is registered under direct_lane(...) instead -- the exact miss that
       left a direct chat unstoppable. */
    expect(calls).toEqual([[
      'turn.cancel',
      { session_key: 's1', target: { agent: 'Raven-Design', handle: 'handle-1' } },
    ]])
  })
})
