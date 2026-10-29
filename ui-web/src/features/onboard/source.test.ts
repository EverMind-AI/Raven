import { expect, it, vi } from 'vitest'

import { onboardSource } from './source'

const calls: Array<[string, unknown]> = []
let warning: string | null = null

vi.mock('../../rpc/gateway', () => ({
  gateway: () => ({
    call: async (method: string, params: unknown) => {
      calls.push([method, params])
      return { applied: true, previous: null, warning }
    },
  }),
}))

it('seeds the Skill Hub endpoint and returns the reload notice the server sends', async () => {
  await onboardSource.seedSkillHub()

  expect(calls).toEqual([['settings.set', { key: 'skillForge.router.hub.init', value: true }]])
  expect(await onboardSource.seedSkillHub()).toBeNull()

  warning = 'Saved. Skill Hub search starts after Raven reloads.'
  expect(await onboardSource.seedSkillHub()).toBe(warning)
})
