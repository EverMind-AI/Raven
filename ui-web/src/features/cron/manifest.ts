/* The cron domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */
import { CronApp } from './CronPage'

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'cron',
  /* A section of the settings dialog rather than a page: #cronBody is the
     box src/App.tsx renders inside it, and this island roots itself in that
     box rather than in this island's tree (features/settings/store.ts's
     HOSTED says why the root stays its own). */
  host: 'cronBody',
  sources: ['cron'],
  root: CronApp,
}
