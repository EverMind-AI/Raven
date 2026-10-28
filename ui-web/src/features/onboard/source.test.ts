import { expect, it, vi } from 'vitest'

import { onboardSource } from './source'

const calls: Array<[string, unknown]> = []

vi.mock('../../rpc/gateway', () => ({
  gateway: () => ({
    call: async (method: string, params: unknown) => {
      calls.push([method, params])
      return { applied: true, previous: null }
    },
  }),
}))

it('seeds the Skill Hub endpoint through settings.set when the wizard finishes', async () => {
  await onboardSource.seedSkillHub()

  expect(calls).toEqual([['settings.set', { key: 'skillForge.router.hub.init', value: true }]])
})
