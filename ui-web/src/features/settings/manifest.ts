/* The settings domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 */
import { SettingsApp } from './SettingsApp'

import type { DomainManifest } from '../manifests'

/* A dialog rather than a page: #spanels is the frame App.tsx renders and this
   island roots its panes in it. The fault banner's source is this domain's
   too -- what it reports is a setting that stopped working. */
export const manifest: DomainManifest = {
  domain: 'settings',
  sources: ['settings', 'banner'],
  root: SettingsApp,
  host: 'spanels',
}
