/* The xa domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */
import { XaApp } from './XaPage'

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'xa',
  page: 'xaPage',
  sources: ['xa'],
  root: XaApp,
}
