/* The composer domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */

import type { DomainManifest } from '../manifests'

/* The dock at the foot of the chat. Its markup is chrome's (src/chrome/Dock.tsx)
   and its listeners are installed by main.tsx, so it roots nothing of its own. */
export const manifest: DomainManifest = {
  domain: 'composer',
  sources: ['composer'],
}
