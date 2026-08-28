/* Full-size view for any image in the page -- a staged thumbnail or one already
 * sent. Clicking anywhere closes it; Escape is wired into the chrome's global
 * chain, which finds the overlay by class rather than asking here.
 *
 * A writer, not an island: the overlay is one node appended to the body, it
 * belongs to no page's root, and both islands that open one (the composer's
 * tray and the transcript's attachment chips) would otherwise each need a root
 * of their own for the same single node.
 *
 * The islands call open() directly rather than through a shell verb. It used to
 * be `Shell.openImage` because the function lived in the legacy layer; now it
 * lives in this bundle, and routing a same-bundle call out through the page and
 * back in would only add a way for it to be missing.
 */

import { t } from './bridge'

const CLS = 'lightbox'

export function close(): void {
  document.querySelectorAll('.' + CLS).forEach((n) => n.remove())
}

export function isOpen(): boolean {
  return !!document.querySelector('.' + CLS)
}

export function open(src: string, name?: string): void {
  /* One at a time: opening a second over the first would leave the first to be
     closed by a click the reader thinks closed the second. */
  close()
  const box = document.createElement('button')
  box.className = CLS
  /* Read at open time, not at module scope: the reader can flip the language
     while the page is up. */
  box.setAttribute('aria-label', t('gui.img.close'))
  const img = document.createElement('img')
  img.src = src
  img.alt = name || ''
  box.appendChild(img)
  box.onclick = close
  document.body.appendChild(box)
  box.focus()
}
