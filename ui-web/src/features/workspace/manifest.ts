/* The workspace domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */

import type { DomainManifest } from '../manifests'

/* The panel mounts lazily: #wsBody is shared ground the browser and sub-agent
   tabs draw into as well, so the workspace root exists only while one of its
   own views is up (features/workspace/store.ts's draw). The desk is a root of
   its own at a standing layer. */
export const manifest: DomainManifest = {
  domain: 'workspace',
  sources: ['workspace', 'prose', 'artifacts'],
}
