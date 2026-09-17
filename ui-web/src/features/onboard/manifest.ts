/* The onboard domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */
import { OnboardApp } from './OnboardPage'

import type { DomainManifest } from '../manifests'

/* #onb is page.html's, not App.tsx's: a portal can only append, and rendered
   from the root it would land after #noJs instead of between the two shells. */
export const manifest: DomainManifest = {
  domain: 'onboard',
  sources: ['onboard'],
  root: OnboardApp,
  host: 'onb',
}
