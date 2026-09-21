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
 * `usage` is the section the page is served on, as the dialog's own install
 * seeded it. `null` is "nothing has asked for a section", which is what a reset
 * test starts from; the island then shows the tab its own state holds. `open`
 * below never touches it.
 */
export const settingsTab: { id: string | null } = { id: 'usage' }

/* What opens the dialog once a section is picked, filled by the page's wiring
   (src/app/install.ts) with the island's own open -- which draws, raises the
   veil and reloads. A slot rather than an import: three domains are sections
   of this dialog now (schedules, channels, memory) and each opens itself
   through `openSection` below, so an import from here would put the settings
   island in every one of their closures. Unfilled it falls through to `open`,
   which is a dialog on whatever the island last drew: enough for a test that
   registered nothing. */
let opener: (() => void) | null = null

/** Registers what raises the dialog for `openSection`. */
export function onOpen(fn: () => void): void {
  opener = fn
}

/* What a section leaves behind: the channel the channels pane was showing, and
   the new-job sheet the schedules section raises over the dialog. Left alone
   both survive a section pick and a close, and come back over whatever the
   reader opens next, still on the entry they were left on -- which is what
   these slots take back. What a slot may NOT do is throw work away: the
   schedules one parks its sheet rather than closing it, because the link a
   reader follows out of that form is inside the form (features/cron/store.ts).
   Each is registered by the domain that owns it, at its own module evaluation,
   because state/ does not import an island; unfilled, a leave asks nothing. */
const SLOTS = ['clearConnChannel', 'parkCronSheet'] as const

/** One of the sheets a section leaves behind. */
export type LeaveSlot = (typeof SLOTS)[number]

const slots = new Map<LeaveSlot, () => void>()

/** Registers what a section change or a close spends on an island. */
export function onLeave(name: LeaveSlot, fn: () => void): void {
  slots.set(name, fn)
}

/** Spends all of them: the island's own section pick calls this. */
export function leaveSection(): void {
  for (const name of SLOTS) slots.get(name)?.()
}

/* What arriving at a section asks of the domain that fills it. Three sections
   are another domain's island (features/settings/store.ts's HOSTED), and each
   holds rows it has to fetch: nothing else on this page would ask for them,
   because the nav row that used to is a section pick now. Keyed by section id
   rather than a fixed list, so a section with nothing to load registers
   nothing and arriving at it asks nothing. */
const enters = new Map<string, () => void>()

/** Registers what a domain does when the dialog arrives at its section. */
export function onEnter(id: string, fn: () => void): void {
  enters.set(id, fn)
}

/** Spends it: the island's own section pick calls this too. */
export function enterSection(id: string): void {
  enters.get(id)?.()
}

/** Opens the dialog on one section, for the domains that are one. */
export function openSection(id: string): void {
  if (settingsTab.id !== id) leaveSection()
  settingsTab.id = id
  enterSection(id)
  if (opener) opener()
  else open()
}

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
  leaveSection()
  markNewCurrent()
}

/* Test seam only: whether the dialog stands open, and which pane it opened on,
   are the module's. */
export function _resetForTests(): void {
  up = false
  settingsTab.id = 'usage'
  opener = null
  slots.clear()
  enters.clear()
}
