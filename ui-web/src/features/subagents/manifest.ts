/* The subagents domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */

import type { DomainManifest } from '../manifests'

/* The roster and the instance panes, drawn into the workspace panel and into
   the desk's canvas (features/subagents/mount.tsx), so they root per pane
   rather than per page. */
export const manifest: DomainManifest = {
  domain: 'subagents',
  sources: ['agents'],
}
