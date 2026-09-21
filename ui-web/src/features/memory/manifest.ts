/* The memory domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */
import { MemoryApp } from './MemoryPage'

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'memory',
  /* `mem*` on 7 of its 13 own classes. */
  cssPrefix: 'mem',
  /* A section of the settings dialog rather than a page: #memoryBody is the
     box src/App.tsx renders inside it, and this island roots itself in that
     box rather than in this island's tree (features/settings/store.ts's
     HOSTED says why the root stays its own). */
  host: 'memoryBody',
  sources: ['memory'],
  root: MemoryApp,
}
