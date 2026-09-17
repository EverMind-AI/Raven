/* A workspace panel for a case that drives an island drawn inside one.
 *
 * The islands ask their panel four things and the panel is handed in rather
 * than imported (src/state/wsPanel.ts says why), so a case that reaches one of
 * those questions has to hand one in too. The default answers "collapsed, on
 * the changes view, nothing picked", which is the state a served page starts
 * in; a case overrides the parts it is about.
 */
import { setWsPanel } from '../state/wsPanel'

import type { WsPanel } from '../state/wsPanel'

export function installWsPanel(over: Partial<WsPanel> = {}): WsPanel {
  const panel: WsPanel = {
    view: () => ({ tab: 'diff', open: false, picked: false }),
    pick: () => {},
    show: () => {},
    setOpen: () => {},
    bump: () => {},
    draw: () => {},
    showsTurn: () => false,
    ...over,
  }
  setWsPanel(panel)
  return panel
}
