// @vitest-environment happy-dom
/* The check's own contract: it answers with a newer version and starts nothing.
 *
 * A47 has two steps -- check, then the row offers the upgrade. This layer used
 * to call `askUpgrade` itself the moment a newer version came back, so one
 * click did two things and the row never got to offer anything. The About page
 * test cannot see this: it installs its own `checkUpdate` through the harness,
 * so the fact lives here, where the seam is implemented.
 */
import { describe, expect, it } from 'vitest'

import { loadPart } from '../../../scripts/module-harness.mjs'

function btn(): HTMLButtonElement {
  const b = document.createElement('button')
  b.textContent = 'check'
  return b
}

describe('the settings chrome update check', () => {
  it('hands back the newer version instead of starting the upgrade', async () => {
    const asked: string[] = []
    const mod = await loadPart(() => import('./wire'), {
      fakes: {
        'src/app/updates': {
          APP_VERSION: '0.2.0',
          appVersionSet: () => {},
          askUpgrade: () => { asked.push('askUpgrade') },
          showUpNote: () => {},
        },
        'src/features/settings/source': { checkVersion: async () => ({ raven_version: '0.2.0', update_available: true, latest_version: '0.3.0' }) },
      },
    })
    const out = await mod.checkUpdate(btn())
    expect(out).toBe('0.3.0')
    expect(asked).toEqual([])
  })

  it('answers null when the build is current', async () => {
    const mod = await loadPart(() => import('./wire'), {
      fakes: {
        'src/app/updates': { APP_VERSION: '0.2.0', appVersionSet: () => {}, askUpgrade: () => {}, showUpNote: () => {} },
        'src/features/settings/source': { checkVersion: async () => ({ raven_version: '0.2.0' }) },
      },
    })
    expect(await mod.checkUpdate(btn())).toBeNull()
  })
})
