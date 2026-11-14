/* The permission chip in the bar under the field, and the click that opens its
 * popover.
 *
 * The whole of it is the store's now: the tier's name, which tier it is, the
 * warning class and the accessible name are one `paint` field state/perm.ts
 * fills in its draw, and aria-expanded is the popover's up-or-down. The four
 * used to be written by id over what this rendered.
 *
 * Until that first draw the chip shows the shape the page is served with -- the
 * plain shield and the served word -- because that is what the region golden
 * records and what a reader sees for the frame before the boot's list runs.
 * The accessible name is the one value with no served form: the page carries no
 * aria-label here, so this renders none until there is a tier to name.
 *
 * The glyph is the popover's own component (`./PermGlyph.tsx`), so the shape on
 * the chip is the shape of the row it names. The served shield above is the one
 * exception, because no tier has drawn yet.
 *
 * The click is the chip's and not the chrome's. It toggles rather than opens,
 * because the popover has no close button of its own and the chip is the only way
 * back out of it with the pointer.
 */

import { useSyncExternalStore } from 'react'

import * as perm from '../state/perm'
import { PermGlyph, SERVED_SHIELD } from './PermGlyph'

import type { JSX } from 'react'

export function PermChip(): JSX.Element {
  const s = useSyncExternalStore(perm.subscribe, perm.get)
  const p = s.paint
  return (
    <button
      className={p?.risk ? 'chip risk' : 'chip'}
      id="permChip"
      aria-expanded={s.open ? 'true' : 'false'}
      aria-haspopup="true"
      aria-label={p ? p.aria : undefined}
      onClick={() => perm.toggle()}
    >
      {p
        ? <PermGlyph id={p.id} size={12} stroke={1.8} className="pico" />
        : (
          <svg
            className="pico"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            aria-hidden="true"
          >
            <path d={SERVED_SHIELD} />
          </svg>
        )}
      <span id="permName">{p ? p.label : 'Smart mode'}</span>
    </button>
  )
}
