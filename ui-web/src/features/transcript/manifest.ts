/* The transcript domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */

import { TurnNav } from './TurnNav'

import type { DomainManifest } from '../manifests'

/* The transcript itself is one root per conversation lane rather than one per
   page (features/transcript/mount.tsx): a lane parked off screen keeps its
   host and loses its interior. The root declared here is the turn nav beside
   it, one per page, in `#turnNav` -- the box src/chrome/ChatTop.tsx renders
   after the scroller and hands over empty. */
export const manifest: DomainManifest = {
  domain: 'transcript',
  sources: ['transcript'],
  root: TurnNav,
  host: 'turnNav',
}
