/* The playbooks domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */
import { PlaybooksApp } from './PlaybooksPage'

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'playbooks',
  /* `pb*` on 43 of its 57 own classes, the board and the editor both. */
  cssPrefix: 'pb',
  page: 'playbooksPage',
  sources: ['playbooks'],
  root: PlaybooksApp,
}
