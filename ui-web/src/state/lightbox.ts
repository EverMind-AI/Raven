/* Full-size view for any image in the page -- a staged thumbnail or one already
 * sent. Clicking anywhere closes it; Escape reaches it through the overlay
 * order (state/escapeOrder.ts), which asks the two verbs below.
 *
 * State rather than a writer: the overlay is one node at the body, it belongs
 * to no page's root, and both islands that open one (the composer's tray and
 * the transcript's attachment chips) would otherwise each need a root of their
 * own for the same single node. What it looks like is src/chrome/Lightbox.tsx;
 * what is here is the one shot on screen.
 *
 * The islands call open() directly: it lives in this bundle, and routing a
 * same-bundle call out through the page and back in would only add a way for it
 * to be missing.
 */

import { t } from '../i18n/t'
import { makeStore } from './store'

export interface Shot {
  readonly src: string
  /** The name the sender gave the file, or '' -- never invented. */
  readonly alt: string
  /* Read when the image is opened, not at render: the reader can flip the
     language while the page is up, and a label that re-read itself would
     change under a shade that is already on screen. */
  readonly label: string
}

const CLS = 'lightbox'

const store = makeStore<Shot | null>(null)

/** The one shot on screen, and the verb that puts it there. */
export const { get, set, subscribe, _resetForTests } = store

export function close(): void {
  set(null)
  /* Anything else carrying the class as well, because that is how the overlay
     order finds an overlay: it asks the document rather than this module, so
     close() has to answer for a node this module did not draw. */
  document.querySelectorAll('.' + CLS).forEach((n) => n.remove())
}

export function isOpen(): boolean {
  return !!document.querySelector('.' + CLS)
}

export function open(src: string, name?: string): void {
  /* One at a time: opening a second over the first would leave the first to be
     closed by a click the reader thinks closed the second. */
  close()
  set({ src, alt: name || '', label: t('gui.img.close') })
  document.querySelector<HTMLElement>('.' + CLS)?.focus()
}
