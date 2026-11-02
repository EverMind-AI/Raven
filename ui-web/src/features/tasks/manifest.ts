/* The tasks domain, as the page registers it.
 *
 * One declaration per domain, read by src/main.tsx (which mounts the roots) and
 * by the two gates that hold the page's tables complete
 * (scripts/gates/domain-shape.test.mjs, domain-registration.test.mjs).
 *
 * One module page, the task board: every conversation's Task Groups, whose
 * root main.tsx mounts into its body. The rest of the domain is drawn in the
 * desk palette and in the panes that palette opens (features/desk/), plus the
 * strip above the composer -- `TasksPanel` is rendered by the palette, not
 * mounted.
 */

import { TasksApp } from './BoardsPage'

import type { DomainManifest } from '../manifests'

export const manifest: DomainManifest = {
  domain: 'tasks',
  page: 'tasksBoardPage',
  /* `tk*` on the rows, the chips and the node card. */
  cssPrefix: 'tk',
  sources: ['tasks'],
  root: TasksApp,
}
