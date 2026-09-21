/* The importSync domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */
import { ImportSyncApp } from './ImportSyncPage'

import type { DomainManifest } from '../manifests'

/* #importRow is inside the rail's foot, which chrome renders (src/chrome/Rail.tsx):
   the slot is the page's furniture and the row in it is this domain's. */
export const manifest: DomainManifest = {
  domain: 'importSync',
  sources: ['importSync'],
  root: ImportSyncApp,
  host: 'importRow',
}
