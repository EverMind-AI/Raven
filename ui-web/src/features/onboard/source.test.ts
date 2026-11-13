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

const NOTICE =
  'Skill Hub search is on: when Raven looks for a skill, it sends text from your conversation to EverMind Skill Hub. You can turn it off in Settings > Skills.'

it('records the Skill Hub opt-in and returns the notice the server sends', async () => {
  await onboardSource.seedSkillHub()

  expect(calls).toEqual([['settings.set', { key: 'skillForge.router.hub.init', value: true }]])
  expect(await onboardSource.seedSkillHub()).toBeNull()

  warning = NOTICE
  expect(await onboardSource.seedSkillHub()).toBe(NOTICE)
})
