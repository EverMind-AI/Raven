/* Whether the settings dialog is up.
 *
 * Settings is a dialog, not a place: it layers over whatever you were reading
 * rather than replacing it, which is why the rail still marks the row you came
 * from every time it opens or shuts.
 *
 * Three verbs in the legacy chrome (setIsOpen / openSet / closeSet,
 * legacy/demo/120-capabilities.js) did this by reading and writing one
 * attribute on div#setVeil. The attribute is still written -- the container is
 * rendered with the flag the page is served with, the CSS shows the
 * dialog from it, and the Escape chain asks this module rather than the
 * element -- but the answer to "is it open" is the flag here, so there is one
 * place that knows and one place that writes.
 *
 * Not the section it opens on: that is src/state/settingsTab.ts, written by
 * whoever asks for a section before opening the dialog, and the island reads it
 * on every draw. openSet never touched it.
 */

import { markNewCurrent } from '../legacy/demo/050-rail.js'

let up = false

/** Whether the dialog is up. Tolerates a page whose markup is not in yet. */
export function isOpen(): boolean {
  return up
}

/* One writer for the flag on the container. */
function paint(): void {
  const el = document.getElementById('setVeil')
  if (el) el.dataset.open = String(up)
}

/* The veil, the rail's marks, then the focus -- openSet's order. The modal
   takes the focus rather than any control inside it, so the first Tab lands on
   the first section and Escape reaches the chrome's chain. */
export function open(): void {
  up = true
  paint()
  markNewCurrent()
  document.getElementById('setModal')?.focus()
}

/** Closing leaves the dialog's contents alone; the island keeps its section. */
export function close(): void {
  up = false
  paint()
  markNewCurrent()
}
