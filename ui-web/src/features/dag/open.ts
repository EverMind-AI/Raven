/* Opening one node of a delegated graph.
 *
 * A node opens where a sub-agent's work already lives, rather than growing a
 * second transcript view inside the sheet: same panel, same renderer, and the
 * sheet stays the map rather than becoming the territory.
 *
 * Its own module because two seams reach it -- the trail card's
 * `sources.transcript.openDagNode` and the sheet's own click -- and because the
 * chrome it opens is the workspace's, not the graph's.
 */

import { panel } from '../../state/wsPanel'
import { openDagNode } from '../subagents/store'

export function dagOpenNode(runId: string, n: { id: string; summary?: string | null }): void {
  openDagNode(runId, n)
  /* The open above already raised the node's own window, and in desk mode that
     window IS the view -- so there is no panel tab left to pick. Picking one
     anyway routed through `openDeskTab`, whose whole job is to open the
     palette, so every node opened from the trail's card or from the sheet
     popped the little desk open beside the window the reader had asked for.
     The lines below are the pre-desk panel, where selecting the agents view
     was how the instance got on screen at all. */
  if (document.documentElement.classList.contains('desk-ready')) return
  const ws = panel()
  if (!ws.view().open) ws.setOpen(true)
  ws.pick('agents')
  ws.draw()
}
