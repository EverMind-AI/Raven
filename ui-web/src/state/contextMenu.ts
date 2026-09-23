/* One rule for right-click everywhere in the window, because the old answer was
 * "whatever that element happened to wire up": a few rows opened the app menu,
 * the transcript swallowed the event entirely, and everything else fell through
 * to the host's own menu -- Look Up / Translate / Services, in the system's
 * language, over a file tree. Three different panels for the same gesture.
 *
 * The rule:
 * 1. In a text field, or over a real text selection, the host menu wins. It has
 *    paste, dictation, spelling and Look Up, and none of that is ours to
 *    reimplement.
 * 2. On a surface that declares actions (a session, a change, a file, a
 *    command, a link), the app menu opens with those actions -- localized, same
 *    panel as the row's own "..." button.
 * 3. Anywhere else, nothing opens. Chrome has no context actions, and a menu
 *    offering only Services is noise.
 *
 * A surface declares its actions by carrying them on `_ctx`: a function, so the
 * rows are built from what is true when the gesture happens rather than from
 * what was true when the surface was drawn. The lookup walks up from the event's
 * target, which is what lets a right-click on any part of a row reach the row.
 */

import { show as menuAt } from './menu'

import type { MenuItem } from './menu'

type Declaring = Element & { _ctx?: () => Array<MenuItem | '-'> }

export function nativeCtxOk(target: Element): boolean {
  if (target.closest('input, textarea, select, [contenteditable="true"]')) return true
  const selection = window.getSelection()
  if (!selection || selection.isCollapsed || !String(selection).trim()) return false
  /* Only when the click is actually inside the selected text -- a selection
     left behind elsewhere on the page is not what this click is about. */
  return selection.containsNode(target, true)
    || (target.contains(selection.anchorNode) && target.contains(selection.focusNode))
}

export function onContextMenu(event: MouseEvent): void {
  const target = event.target as Element
  if (nativeCtxOk(target)) return
  event.preventDefault()
  /* The first surface on the way up that declares anything answers, whether or
     not it has rows to offer: a row that decides it has none is still the
     answer, and the next surface out is not asked. */
  for (let node: Declaring | null = target; node; node = node.parentElement) {
    if (!node._ctx) continue
    const items = node._ctx()
    if (items && items.length) menuAt(event.clientX, event.clientY, items)
    return
  }
}
