/* The browser domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */

import type { DomainManifest } from '../manifests'

/* The screencast and the link list, drawn into the workspace panel by the
   workspace's own draw (features/browser/mount.tsx) rather than into a box of
   its own. */
export const manifest: DomainManifest = {
  domain: 'browser',
  sources: ['browser'],
}
