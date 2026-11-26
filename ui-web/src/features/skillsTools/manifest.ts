/* The Skills & Tools domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the root into
 * #skillsToolsBody) and by scripts/gates/domain-shape.test.mjs and
 * domain-registration.test.mjs.
 */
import { SkillsToolsApp } from './SkillsToolsApp'

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'skillsTools',
  page: 'skillsToolsPage',
  sources: ['skillsTools'],
  root: SkillsToolsApp,
}
