import { beforeEach, describe, expect, it } from 'vitest'

import type { InstanceRow } from '../rpc/generated.js'

import { getDirectChat, patchDirectChat, resetDirectChat } from '../app/directChatStore.js'
import { fetchInstances } from '../app/directChatSync.js'

const row = (agent: string, handle: string): InstanceRow =>
  ({ agent, createdAtMs: 1, handle, kind: 'acp', sessionKey: 's1', status: 'completed', updatedAtMs: 1 }) as InstanceRow

beforeEach(() => {
  resetDirectChat()
})

describe('fetchInstances', () => {
  it('replaces the strip with what the runtime reports', async () => {
    await fetchInstances(async () => ({ instances: [row('A', 'one')], pending_handoff_count: 2 }) as never, 's1')

    expect(getDirectChat().instances.map(r => r.handle)).toEqual(['one'])
    expect(getDirectChat().pendingHandoffCount).toBe(2)
  })

  it('keeps the strip when the read fails', async () => {
    // A quiet rpc answers null for a failed call, and emptying on that made
    // every chip vanish for one refresh interval while the instances were
    // still there.
    patchDirectChat({ instances: [row('A', 'one'), row('B', 'two')] })
    await fetchInstances(async () => null, 's1')

    expect(getDirectChat().instances.map(r => r.handle)).toEqual(['one', 'two'])
  })

  it('keeps the strip when the read throws', async () => {
    patchDirectChat({ instances: [row('A', 'one')] })
    await fetchInstances(async () => {
      throw new Error('gateway went away')
    }, 's1')

    expect(getDirectChat().instances.map(r => r.handle)).toEqual(['one'])
  })

  it('still empties the strip when the session genuinely has none', async () => {
    // Paired with the case above: "the call failed" and "there is nothing" have
    // to stay distinguishable, or the strip keeps a dead session's rows.
    patchDirectChat({ instances: [row('A', 'one')] })
    await fetchInstances(async () => ({ instances: [], pending_handoff_count: 0 }) as never, 's1')

    expect(getDirectChat().instances).toEqual([])
  })

  it('clears the strip when there is no session key', async () => {
    patchDirectChat({ instances: [row('A', 'one')] })
    await fetchInstances(async () => ({ instances: [row('A', 'one')] }) as never, null)

    expect(getDirectChat().instances).toEqual([])
  })
})
