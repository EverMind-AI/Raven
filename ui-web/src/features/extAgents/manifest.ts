/* The extAgents domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 *
 * One name this domain still answers to, and the reason. Its message keys
 * are `gui.agent.*` -- the namespace that reads as features/subagents/'s and
 * is this domain's: the catalogue is i18n/messages.json at the REPO root,
 * which the TUI generates its own copy from, so renaming a namespace is an
 * edit to both front ends rather than to this directory (no TUI source reads
 * this one -- measured -- so the rename is safe, just not this tree's to make;
 * scripts/gates/i18n-keys.test.mjs carries the mapping meanwhile). Its classes
 * carry the domain's own prefix, so there is no `cssPrefix` to declare.
 */
import { ExtAgentsApp } from './ExtAgentsPage'

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'extAgents',
  page: 'extAgentsPage',
  sources: ['extAgents'],
  root: ExtAgentsApp,
}
