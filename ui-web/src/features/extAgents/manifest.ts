/* The extAgents domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 *
 * Two names this domain still answers to, and one reason each. Its message
 * keys are `gui.agent.*` -- the namespace that reads as features/subagents/'s
 * and is this domain's: the catalogue is i18n/messages.json at the REPO root,
 * which the TUI generates its own copy from, so renaming a namespace is an
 * edit to both front ends rather than to this directory (no TUI source reads
 * this one -- measured -- so the rename is safe, just not this tree's to make;
 * scripts/gates/i18n-keys.test.mjs carries the mapping meanwhile). And its
 * detail-card button is `.xaedit`, which src/styles/page.css carries five
 * rules for, so the class prefix waits on the CSS step the way knowledge's
 * page id does.
 */
import { ExtAgentsApp } from './ExtAgentsPage'

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'extAgents',
  page: 'extAgentsPage',
  sources: ['extAgents'],
  root: ExtAgentsApp,
}
