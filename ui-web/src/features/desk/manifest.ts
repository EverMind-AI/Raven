/* The desk domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */

import type { DomainManifest } from '../manifests'

/* The floating desk: a standing layer of its own rather than a module page, so
   it owns no page row and no rail button. Its root is not mounted by the loop
   in src/main.tsx either -- the layer it goes in is made by state/portals.ts on
   demand, and the render waits a microtask so the first frame belongs to the
   page. It answers no seam: what a pane shows is read through the workspace's
   source, the way features/skills/ reads features/plugins/. */
export const manifest: DomainManifest = {
  domain: 'desk',
  sources: [],
}
