/* A page with no provider configured opens on the onboarding wizard, and the
 * task actions still send a reader who left it to Models.
 *
 * Nothing about it is visible in a rendered tree: the wizard is an island in
 * #onb and the redirect is a flag three call sites read. Two things can be
 * forgotten -- the boot deciding first run from `setup.status` and opening the
 * wizard on it, and a Send that starts a turn no provider can answer once the
 * wizard is closed -- and both are one line each.
 *
 * Until the wizard was rebuilt after the first-run prototype the boot only
 * recorded the flag and left the page available, with the redirect as the
 * whole first-run experience; this gate pinned that. The wizard is the first
 * screen now, the redirect stays for the page behind it, and the wizard
 * re-reads `setup.status` into the same flag when it closes
 * (features/onboard/store.ts), so a reader who just connected a provider is
 * not sent to Settings on their first Send.
 */

import { describe, expect, it } from 'vitest'

import { moduleText } from '../module-harness.mjs'

/* The three places the redirect is decided, as one text: which module a line
   sits in is not what this is about. */
const wiring = moduleText('app/boot.ts') + moduleText('app/install.ts') + moduleText('chrome/ModelChip.tsx')

describe('first-run model setup', () => {
  it('opens the wizard on a first run, and records the flag the redirect reads', () => {
    expect(wiring).toContain("const firstRun = setup.provider_configured === false")
    expect(wiring).toContain('setupState.providerConfigured = !firstRun')
    expect(wiring).toMatch(/if \(!cannedInstead && \(firstRun \|\| \/\[\?&\]onboard=1\/\.test\(location\.search\)\)\) \{\s*openOnboard\(\)/)
  })

  it('guards New Task, Send, and the model selector with the same redirect', () => {
    expect(wiring).toContain('.beforeSend = openModelsForMissingProvider')
    expect(wiring.match(/if \(openModelsForMissingProvider\(\)\) return/g)).toHaveLength(2)
  })

  it('re-reads the verdict when the wizard closes', () => {
    expect(moduleText('features/onboard/store.ts')).toContain('setupState.providerConfigured = await source().providerConfigured()')
  })
})
