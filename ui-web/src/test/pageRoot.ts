/* The page's own root, for a test whose subject draws through it.
 *
 * It renders the page: src/App.tsx is one portal at the body carrying every
 * region src/page.html used to be, so mounting this gives a case the real
 * chrome -- the dialogs, the rail, the dock, the seven module pages, the
 * standing #menu and #toasts hosts -- the way src/main.tsx stands it up before
 * anything else. A case whose own fixture carried one of those elements would
 * then have two of it, so the fixture is what goes.
 *
 * The root itself is detached, which is the mechanism: React clears a container
 * it is given as a root, so a root AT the body would delete what the document
 * was served with instead of joining it.
 *
 * The menu and the notices find their host when they are raised, not when this
 * renders, so the order does not matter for them -- markup first or root first,
 * either way.
 */
import { createElement } from 'react'
import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'

import { App } from '../App'

/** Renders the root synchronously; the return value takes it back down. */
export function mountPageRoot(): () => void {
  const root = createRoot(document.createElement('div'))
  flushSync(() => root.render(createElement(App)))
  return () => root.unmount()
}
