/* A page with no provider configured sends every task action to Models.
 *
 * Nothing about it is visible in a rendered tree: the page stays available on
 * first run, and what changes is where New Task, Send and the model selector
 * go. Three call sites and one flag, and the failure is one of them being
 * forgotten -- a Send that starts a turn no provider can answer.
 *
 * Split out of boot-order.test.mjs, where it rode along with the boot sequence
 * and the permission chip. Same assertions; what they read is now only the two
 * modules that carry them (src/app/install.ts wires the composer and the rail
 * button, src/features/model/chip.ts the selector) plus the boot step that sets
 * the flag.
 */

import { describe, expect, it } from 'vitest'

import { moduleText } from '../module-harness.mjs'

/* The three places the redirect is decided, as one text: which module a line
   sits in is not what this is about. */
const wiring = moduleText('app/boot.ts') + moduleText('app/install.ts') + moduleText('features/model/chip.ts')

describe('first-run model setup', () => {
  it('records missing-provider state without opening onboarding automatically', () => {
    expect(wiring).toContain('setupState.providerConfigured = setup.provider_configured !== false')
    expect(wiring).not.toMatch(/setup\.provider_configured === false\s*\|\|/)
  })

  it('guards New Task, Send, and the model selector with the same redirect', () => {
    expect(wiring).toContain('.beforeSend = openModelsForMissingProvider')
    expect(wiring.match(/if \(openModelsForMissingProvider\(\)\) return/g)).toHaveLength(2)
  })
})
