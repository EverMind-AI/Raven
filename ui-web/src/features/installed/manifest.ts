/* The installed domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */

import type { DomainManifest } from '../manifests'

/* No island: one shared read of `ext.list` that the two capability tabs, the
   memory page and the settings dialog all draw their rows from. */
export const manifest: DomainManifest = {
  domain: 'installed',
  sources: ['capabilities'],
}
