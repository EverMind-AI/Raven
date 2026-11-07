/* The imported domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs). This one
 * has no root: its button is the rail's chrome and its rows are the rail's.
 */

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'imported',
  sources: ['imported'],
}
