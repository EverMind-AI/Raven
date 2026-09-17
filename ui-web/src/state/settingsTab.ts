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
 * test starts from; the island then shows the tab its own state holds.
 */
export const settingsTab: { id: string | null } = { id: 'usage' }
