/* The language store, under the name every region already imports.
 *
 * The store and nothing else. `./effects` imports the island stores a repaint
 * reaches and an island's own store imports the language, so a barrel that also
 * carried the effects would close SettingsPage -> lang -> effects ->
 * features/settings/store into a cycle that runs while those modules are still
 * evaluating, which is exactly the shape src/state/wsPanel.ts exists to
 * avoid. The two halves that are not the store -- the whole-page repaint and
 * the pick that persists -- are asked for by name: `./effects` from
 * src/main.tsx, `./pick` from the boot and the settings chrome.
 */

export * from './store'
