/* The settings dialog's page-wide state: whether it is up, and the section it
 * opens on.
 *
 * Two slots rather than one module each, because they have the same two sides:
 * a page-wide writer sets the section and THEN opens the dialog, and the
 * island reads both on every draw.
 *
 * Settings is a dialog, not a place: it layers over whatever you were reading
 * rather than replacing it, which is why the rail still marks the row you came
 * from every time it opens or shuts.
 *
 * Three verbs (setIsOpen / openSet / closeSet) used to do this by reading and
 * writing one attribute on div#setVeil. The attribute is still written -- the
 * container is
 * rendered with the flag the page is served with, the CSS shows the
 * dialog from it, and the Escape chain asks this module rather than the
 * element -- but the answer to "is it open" is the flag here, so there is one
 * place that knows and one place that writes.
 */

import { markNew as markNewCurrent } from '../features/rail/store'

/* Which section the settings dialog opens on.
 *
 * Not in the settings island's own store, because the two writers are not the
 * island: a caller sets it and THEN opens the dialog (the slash command's
 * "manage models"), and the page serves it seeded. The island reads it on every
 * draw and writes it when a reader picks a section, so the slot has to be
 * somewhere both sides can reach -- which used to mean window.sTab.
 *
 * `usage` is the section the page is served on, as the legacy shell's install
 * seeded it. `null` is "nothing has asked for a section", which is what a reset
 * test starts from; the island then shows the tab its own state holds. `open`
 * below never touches it.
 */
export const settingsTab: { id: string | null } = { id: 'usage' }

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
