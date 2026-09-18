/* The tasks domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 *
 * No `page`: tasks is not a module page. It is drawn in the desk palette and in
 * the panes that palette opens (features/desk/), plus the strip above the
 * composer -- so there is no `<section>` for state/pages.ts to name and no root
 * for main.tsx to mount. `TasksApp` is rendered by the palette.
 */

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'tasks',
  /* `tk*` on the rows, the chips and the node card. */
  cssPrefix: 'tk',
  sources: ['tasks'],
}
