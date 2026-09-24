/* The dag domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */

import type { DomainManifest } from '../manifests'

/* The delegated graph: a card inside the transcript, and the run's own state
   (features/dag/mount.ts) that session resume puts back on reload -- a pane's
   graph comes from the desk's own `tasks.list` read instead. It renders
   nothing of its own and answers no seam -- what the card draws arrives on
   the turn's own events. */
export const manifest: DomainManifest = {
  domain: 'dag',
  sources: [],
}
