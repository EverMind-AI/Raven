/* The full-size image over the page (state/lightbox.ts).
 *
 * A portal at the body rather than into a layer of its own: the overlay is one
 * node that belongs to no page, and the body is where it has always been
 * appended -- a wrapper would take the `position: fixed` inset the class
 * carries, and the node would no longer be the body's own child that
 * state/portals.ts files it as.
 *
 * The whole overlay is the close button, which is why the image sits inside
 * one: a click anywhere on it, including on the picture, takes it down.
 */
import { useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import * as lightbox from '../state/lightbox'

import type { JSX } from 'react'

export function Lightbox(): JSX.Element | null {
  const shot = useSyncExternalStore(lightbox.subscribe, lightbox.get)
  if (!shot) return null
  return createPortal(
    <button className="lightbox" aria-label={shot.label} onClick={() => lightbox.close()}>
      <img src={shot.src} alt={shot.alt} />
    </button>,
    document.body
  )
}
