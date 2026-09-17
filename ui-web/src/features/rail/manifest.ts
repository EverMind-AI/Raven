/* The rail domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */
import { RailApp } from './RailPage'

import type { DomainManifest } from '../manifests'

/* #list is inside the rail, which chrome renders (src/chrome/Rail.tsx): the
   column is the page's furniture and the rows in it are this domain's. */
export const manifest: DomainManifest = {
  domain: 'rail',
  sources: ['sessions'],
  root: RailApp,
  host: 'list',
}
