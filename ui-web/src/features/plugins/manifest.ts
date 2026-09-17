/* The plugins domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */

import type { DomainManifest } from '../manifests'

/* One of the two tabs of the capabilities page, which is chrome's
   (src/chrome/CapsPage.tsx): this island renders into a detached host the tab
   re-attaches on every draw (features/hosts.ts). */
export const manifest: DomainManifest = {
  domain: 'plugins',
  /* `pm*` -- the plugin-manager rows, 11 of its 14 own classes; the rest of
   that family is shared with skills and sits in the gate's LEGACY_SHARED. */
  cssPrefix: 'pm',
  sources: ['plugins'],
}
