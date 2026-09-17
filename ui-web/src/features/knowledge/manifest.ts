/* The knowledge domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */
import { KnowledgeApp } from './KnowledgePage'

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'knowledge',
  page: 'kbPage',
  sources: ['knowledge'],
  root: KnowledgeApp,
}
