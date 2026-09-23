/* A workspace pane for a case that drives an island drawn inside one.
 *
 * The islands ask their pane four things and the pane is handed in rather
 * than imported (src/state/wsPane.ts says why), so a case that reaches one of
 * those questions has to hand one in too. The default answers "collapsed, on
 * the changes view, nothing picked", which is the state a served page starts
 * in; a case overrides the parts it is about.
 */
import { setWsPane } from '../state/wsPane'

import type { WsPane } from '../state/wsPane'

export function installWsPane(over: Partial<WsPane> = {}): WsPane {
  const pane: WsPane = {
    view: () => ({ tab: 'diff', open: false, picked: false }),
    pick: () => {},
    show: () => {},
    setOpen: () => {},
    bump: () => {},
    draw: () => {},
    showsTurn: () => false,
    ...over,
  }
  setWsPane(pane)
  return pane
}
