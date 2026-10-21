/* Whether the rail stands or is collapsed.
 *
 * Three writes and no state was all this used to be, so "is the rail open" was
 * readable only by asking the DOM what
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
 * (src/chrome/behaviour/panes.ts), and the session list inside it, which is an island
 * (src/features/rail/).
 */

/** Served standing: page.html carries no data-rail, and the boot applies open. */
let open = true

/** Whether the rail stands. */
export function get(): boolean {
  return open
}

/* The width below which the rail cannot share the window with the content.
   The same number as the `@media` block in src/styles/page.css that folds the
   shell there: one column, the rail an overlay over it when it stands, and no
   floor under the body -- so the two have to move together. 640 is where the
   content beside a rail at its narrowest (190px) stops being usable. */
export const FOLDS = '(max-width: 639px)'

/** The state the boot applies: standing, unless the window folds the shell. */
export function stands(): boolean {
  return !matchMedia(FOLDS).matches
}

/* Shrinking the window under the fold takes a standing rail down, so the
   content is what the reader keeps. The same shape as the workspace pane's
   split point (state/ws.ts, `watchNarrow`): what a match does is this module's,
   the moment it is watched from is src/main.tsx's. No initial check -- the
   boot asks `stands()` for the first paint -- and nothing on the way back up:
   a rail the window took down comes back by the twin, like one the reader
   took down. The query is handed back for the caller to hold. */
export function watchNarrow(): MediaQueryList {
  const query = matchMedia(FOLDS)
  query.addEventListener('change', (e) => {
    if (e.matches && open) set(false)
  })
  return query
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

/* Test seam only: whether the rail stands open is the module's. */
export function _resetForTests(): void {
  open = true
}
