// @vitest-environment happy-dom
/* The check's own contract: it records the newer version and starts nothing.
 *
 * A47 has two steps -- check, then the row offers the upgrade. This layer used
 * to call `askUpgrade` itself the moment a newer version came back, so one
 * click did two things and the row never got to offer anything. The About page
 * test cannot see this: it installs its own `checkUpdate` through the harness,
 * so the fact lives here, where the seam is implemented.
 *
 * What the check leaves behind is asserted where the page reads it -- the
 * version `showUpNote` retains -- rather than as a return value, because the
 * row that asked for the check is gone by the time it resolves: the redraw the
 * check does replaces it.
 */
import { describe, expect, it } from 'vitest'

import { loadPart } from '../../../scripts/module-harness.mjs'

function btn(): HTMLButtonElement {
  const b = document.createElement('button')
  b.textContent = 'check'
  return b
}

describe('the settings chrome update check', () => {
  it('records the newer version instead of starting the upgrade', async () => {
    const asked: string[] = []
    const noted: Array<[string, string | null | undefined]> = []
    const mod = await loadPart(() => import('./wire'), {
      fakes: {
        'src/app/updates': {
          APP_VERSION: '0.2.0',
          appVersionSet: () => {},
          askUpgrade: () => { asked.push('askUpgrade') },
          showUpNote: (kind: string, latest?: string | null) => { noted.push([kind, latest]) },
          upgradeLatest: () => null,
        },
        'src/features/settings/source': { checkVersion: async () => ({ raven_version: '0.2.0', update_available: true, latest_version: '0.3.0' }) },
      },
    })
    await mod.checkUpdate(btn())
    /* The notice is where the version lives on: `upgradeLatest` reads it back
       and the About row draws the second step from that. */
    expect(noted).toEqual([['ver', '0.3.0']])
    expect(asked).toEqual([])
  })

  it('records nothing when the build is current', async () => {
    const noted: string[] = []
    const mod = await loadPart(() => import('./wire'), {
      fakes: {
        'src/app/updates': {
          APP_VERSION: '0.2.0',
          appVersionSet: () => {},
          askUpgrade: () => {},
          showUpNote: (kind: string) => { noted.push(kind) },
          upgradeLatest: () => null,
        },
        'src/features/settings/source': { checkVersion: async () => ({ raven_version: '0.2.0' }) },
      },
    })
    const b = btn()
    await mod.checkUpdate(b)
    expect(noted).toEqual([])
    /* The catalogue's own word: this harness loads the real one. */
    expect(b.textContent).toBe('Up to date')
  })
})
