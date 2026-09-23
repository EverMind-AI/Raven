/* The widths the reader dragged the two resizable panes to, as their grips
 * persist them (chrome/behaviour/panes.ts writes them). Kept here rather than
 * beside the grips so a feature can read one without importing the chrome:
 * the desk asks for the workspace column's width when it opens a pane.
 */

export type PaneName = 'rail' | 'ws'

export const PANE_KEY: Record<PaneName, string> = {
  rail: 'raven.gui.railw',
  ws: 'raven.gui.wsw',
}

/* The width the reader last dragged a pane to, or null when they never have.
   Only a drag or the grip's arrows persist one; the widths the desk picks for
   itself on open are not written, so this is always the reader's own choice. */
export function storedWidth(name: PaneName): number | null {
  try {
    const v = parseFloat(localStorage.getItem(PANE_KEY[name]) || '')
    return Number.isFinite(v) && v > 0 ? v : null
  } catch {
    return null
  }
}
