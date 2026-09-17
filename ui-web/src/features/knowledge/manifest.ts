/* The knowledge domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 *
 * The one domain whose page is still named by an abbreviation: `#kbPage` is
 * what src/styles/page.css scopes two rules to, so renaming the id would move
 * the stylesheet, and the three ids are one set -- a page, its body and the
 * rail button that lights it -- so half a rename would leave the domain with
 * two prefixes instead of one. The CSS step is what moves those rules into the
 * domain's own sheet, and `kbPage` / `kbBody` / `kbBtn` rename with them. The
 * `kb` class prefix in scripts/check-class-namespace.mjs is the same wait.
 */
import { KnowledgeApp } from './KnowledgePage'

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'knowledge',
  page: 'kbPage',
  sources: ['knowledge'],
  root: KnowledgeApp,
}
