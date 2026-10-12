/* The trajectory domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 *
 * No `page`: the trajectory is the chat column's second view, not a module
 * page. Its root goes into `#trajHost`, the box src/chrome/ChatTop.tsx renders
 * beside the scroller and hands over empty; the header's toggle decides which
 * of the two is on screen.
 */

import { TrajectoryApp } from './TrajectoryApp'

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'trajectory',
  sources: ['trajectory'],
  root: TrajectoryApp,
  host: 'trajHost',
}
