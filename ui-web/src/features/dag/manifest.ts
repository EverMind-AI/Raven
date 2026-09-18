/* The dag domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */

import type { DomainManifest } from '../manifests'

/* The delegated graph: a sheet in the rack above the composer, mounted per run
   by features/dag/mount.tsx, and a card inside the transcript. It answers no
   seam of its own -- what it draws arrives on the turn's own events. */
export const manifest: DomainManifest = {
  domain: 'dag',
  sources: [],
}
