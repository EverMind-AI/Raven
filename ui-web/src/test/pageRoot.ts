/* The page's own root, for a test whose subject draws through it.
 *
 * Four overlays render from src/App.tsx rather than from the module that raises
 * them -- the confirm sheet, the settings frame, the context menu and the
 * notices -- so a test that asks one of those modules for DOM has to have the
 * root standing, the way src/main.tsx stands it up before anything else.
 *
 * It renders only into the containers the test's own markup provides, and the
 * root itself is detached, so a page with none of them gets nothing: mounting
 * this cannot add an element to the body or change what a snapshot of one
 * records.
 *
 * The menu and the notices find their host when they are raised, not when this
 * renders, so the order does not matter for them -- markup first or root first,
 * either way. The three dialog interiors portal into the container that exists
 * when this renders, which is what src/page.html guarantees in the page.
 */
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'

import { App } from '../App'

/** Renders the root synchronously; the return value takes it back down. */
export function mountPageRoot(): () => void {
  const root = createRoot(document.createElement('div'))
  flushSync(() => root.render(createElement(App)))
  return () => root.unmount()
}
