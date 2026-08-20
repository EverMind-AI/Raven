/* The click a prose chip carries.
 *
 * The renderer next door (shell/prose.ts) decides which strings in an answer
 * earn a chip -- `code.pth` for a path it could resolve, `.artf` for a
 * deliverable the author linked by hand -- and emits them with the resolved
 * path on `data-p`. Until now the other half of that contract lived in the
 * legacy shell: two document-level listeners in demo/150-chrome.js, calling a
 * pair of function bindings (pathOpen, dirOpen) that the live layer overwrote
 * at load. So the renderer and the click were in different layers, and which
 * one you got depended on which file had run last.
 *
 * A writer, not an island: what it owns is two listeners on the document, and
 * the nodes it acts on are drawn by whoever rendered the prose. Delegation
 * rather than per-chip handlers because prose is replaced wholesale on every
 * answer -- there is nothing stable to bind to.
 *
 * What opening MEANS is still the page's, through DS.prose.open: the live
 * layer shows the file in the workspace island, the offline demo just brings
 * its file view up. This module only turns a click into a resolved target.
 */

import { ds } from './bridge'

import type { ProseSource, ProseTarget } from './prose'

const source = (): ProseSource => ds<ProseSource>('prose')

/* The chip is the record: prose.ts wrote the resolved path onto data-p, and
   `data-d` marks the ones that resolved to a folder. Reading the DOM back is
   what lets one listener serve every chip on the page. */
function targetOf(node: HTMLElement): ProseTarget | null {
  const p = node.dataset.p
  return p ? { p, dir: !!node.dataset.d } : null
}

/* One test, where the legacy pair used two that disagreed: `code.pth` on the
   click, `classList.contains('pth')` on the key. The transcript island draws
   its path chips as buttons, so only the second of those ever matched them --
   and this selector matches what that one matched, keyboard behaviour
   included. The click half stays the island's own either way, because React
   stops the native event when it stops the synthetic one. */
const chipAt = (node: EventTarget | null): HTMLElement | null => {
  const el = node as Element | null
  if (!el || !el.closest) return null
  return el.closest<HTMLElement>('.pth, .artf')
}

export function open(target: ProseTarget): void {
  source().open?.(target)
}

export function install(): void {
  document.addEventListener('click', (e) => {
    const chip = chipAt(e.target)
    const at = chip && targetOf(chip)
    if (at) open(at)
  })

  /* A chip is a link, so it answers the keys a link answers. The chips prose.ts
     emits carry tabindex and role themselves; this is the other half of that,
     and preventDefault is what stops Space from scrolling the transcript out
     from under the file that is about to open. */
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return
    const chip = chipAt(document.activeElement)
    const at = chip && targetOf(chip)
    if (!at) return
    e.preventDefault()
    open(at)
  })
}
