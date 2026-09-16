/* Which section the settings dialog opens on.
 *
 * Not in the settings island's own store, because the two writers are not the
 * island: the legacy chrome sets it and THEN opens the dialog (the slash
 * command's "manage models", ui-web/src/legacy/demo/150-chrome.js), and the
 * demo shell seeds it at install. The island reads it on every draw and writes
 * it when a reader picks a section, so the slot has to be somewhere both sides
 * can reach -- which used to mean window.sTab.
 *
 * `null` is "nothing has asked for a section", which is what a fresh page and a
 * reset test both start from; the island then shows the tab its own state
 * holds.
 */
export const settingsTab: { id: string | null } = { id: null }
