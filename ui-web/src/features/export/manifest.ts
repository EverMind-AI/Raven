/* The export domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */
import { ExportApp } from './ExportApp'

import type { DomainManifest } from '../manifests'

/* #exportBody is inside the #exportVeil dialog, which src/App.tsx renders: the
   dialog's frame is the page's furniture and the wizard in it is this domain's. */
export const manifest: DomainManifest = {
  domain: 'export',
  sources: ['export'],
  root: ExportApp,
  host: 'exportBody',
}
