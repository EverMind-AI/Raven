/* The model domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */

import type { DomainManifest } from '../manifests'

/* The model picker is a popover anchored to whatever opened it, so its root is
   a standing layer at the body rather than a box inside a page (src/main.tsx,
   state/portals.ts). The tier chip's source is this domain's too: a rung is a
   model decision. */
export const manifest: DomainManifest = {
  domain: 'model',
  sources: ['model', 'tier'],
}
