/* Whether the rail stands or is collapsed.
 *
 * Was setRail in the legacy chrome (legacy/demo/150-chrome.js): three writes
 * and no state, so "is the rail open" was readable only by asking the DOM what
 * `.app` carried -- which is what the keyboard shortcut for the rail did, and
 * why it reads the flag here instead now.
 *
 * The three writes stay, because none of the elements they land on belongs to
 * the rail's own markup: `.app` and `<html>` are the grid and the root that the
 * stylesheet reads the state off (`.app[data-rail="off"] .rail{display:none}`,
 * src/styles/page.css), and button#railShow is a top-level region of its own.
 * All three are rendered by src/App.tsx with the value the page is served with
 * and never written by it again -- React diffs against the props it rendered
 * last rather than against the document -- so one value keeps one writer, and
 * this is it.
 *
 * Not here: the rail's WIDTH, which is a drag rather than a state
 * (src/shell/panes.ts), and the session list inside it, which is an island
 * (src/features/rail/).
 */

/** Served standing: page.html carries no data-rail, and the boot applies open. */
let open = true

/** Whether the rail stands. */
export function get(): boolean {
  return open
}

/* The writes setRail made, in setRail's order. Unguarded, as it was: every one
   of these three elements is static markup the page always has, and a missing
   one is a boot step that failed loudly rather than a rail half collapsed. */
export function set(on: boolean): void {
  open = on
  const app = document.querySelector('.app') as HTMLElement
  app.dataset.rail = on ? 'on' : 'off'
  document.documentElement.dataset.rail = on ? 'on' : 'off'
  ;(document.getElementById('railShow') as HTMLElement).hidden = on
}
