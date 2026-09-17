/* What the workspace panel shows, for the islands drawn inside it.
 *
 * The panel's own state and chrome are src/state/ws.ts, and that module reads
 * every island it draws -- the workspace's record, the browser's frames, the
 * delegated rows, the desk. An island importing it back would put its own
 * initialisation inside that cycle, and two modules in there do real work as
 * they evaluate: features/workspace/deskStore.ts builds its first state from
 * the workspace store, and the desk and the sub-agents store open things in
 * each other. A cycle is harmless when every read is inside a function; those
 * are not.
 *
 * So the four questions an island has for its panel are declared here, in a
 * module that imports nothing, and src/main.tsx hands the panel in. The same
 * inversion src/islands.ts makes for the other direction, and the same one
 * features/workspace/store.ts's setDeskOpener makes inside the feature.
 */

/** What the pane's chrome currently shows. */
export interface WsPanelView {
  tab: string
  open: boolean
  picked: boolean
}

export interface WsPanel {
  /** Which view, whether the pane stands, and whether a reader chose it. */
  view(): WsPanelView
  /** Pick a view. The panel decides whether that means the desk instead. */
  pick(tab: string): void
  /** Put a view on screen: open the pane if it is shut, then pick. */
  show(tab: string): void
  /** Open or collapse the pane, and optionally pick a view with it. */
  setOpen(open: boolean, view?: string): void
  /** Re-count the unseen changes on the header chip. */
  bump(): void
  /** Redraw whichever view is up. */
  draw(): void
  /** Whether the pane is showing the turn the record is writing into. */
  showsTurn(): boolean
}

let wired: WsPanel | null = null

/** src/main.tsx, once. */
export function setWsPanel(p: WsPanel): void {
  wired = p
}

/** The panel, or a loud failure: an island runs inside the assembled page. */
export function panel(): WsPanel {
  if (!wired) throw new Error('the workspace panel is not wired')
  return wired
}

/** Tests only: back to the state a fresh page starts in. */
export function _resetForTests(): void {
  wired = null
}
