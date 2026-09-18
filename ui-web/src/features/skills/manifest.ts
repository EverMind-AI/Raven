/* The skills domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */

import type { DomainManifest } from '../manifests'

/* The other tab of the capabilities page, mounted the same way the plugin tab
   is: a detached host the tab re-attaches, plus its own skeleton
   (features/hosts.ts). */
export const manifest: DomainManifest = {
  domain: 'skills',
  sources: ['skills'],
}
