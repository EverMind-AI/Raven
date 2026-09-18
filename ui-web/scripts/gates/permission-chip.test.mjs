/* The permission chip mirrors every settings load.
 *
 * The push lives outside loadSettings, so each caller must invoke it itself --
 * boot included, or a cold page shows the localStorage cache while the gate
 * enforces the server's mode. Two of the three callers are the settings
 * source's own verbs, so its text is read beside the boot's.
 *
 * Split out of boot-order.test.mjs, where it rode along with the boot sequence
 * and the first-run redirect. Same assertion, over the two modules that carry
 * the call sites rather than over the whole of the page's wiring.
 */

import { describe, expect, it } from 'vitest'

import { moduleText } from '../module-harness.mjs'

const wiring = moduleText('app/boot.ts') + moduleText('features/settings/source.ts')

describe('the permission chip mirrors every settings load', () => {
  it('each loadSettings call site pushes the mode afterwards', () => {
    const callers = [...wiring.matchAll(/(?<!function )loadSettings\(\)/g)].length
    const pushes = [...wiring.matchAll(/pushPermMode\(\)|\.then\(pushPermMode\)/g)].length
    expect(callers).toBeGreaterThanOrEqual(3)
    expect(pushes).toBe(callers)
  })
})
