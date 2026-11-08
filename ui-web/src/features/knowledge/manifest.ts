/* The knowledge domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots)
 * and by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 *
 * Its classes carry the domain's own prefix, so there is no `cssPrefix` to
 * declare.
 */
import { KnowledgeApp } from './KnowledgePage'

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'knowledge',
  page: 'knowledgePage',
  sources: ['knowledge'],
  root: KnowledgeApp,
}
