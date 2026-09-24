/* The permission chip in the bar under the field, and the click that opens its
 * popover.
 *
 * The whole of it is the store's now: the tier's name, its shield, the warning
 * class and the accessible name are one `paint` field state/perm.ts fills in
 * its draw, and aria-expanded is the popover's up-or-down. The four used to be
 * written by id over what this rendered.
 *
 * Until that first draw the chip shows the shape the page is served with -- the
 * plain shield and the served word -- because that is what the region golden
 * records and what a reader sees for the frame before the boot's list runs.
 * The accessible name is the one value with no served form: the page carries no
 * aria-label here, so this renders none until there is a tier to name.
 *
 * The icon goes in as markup because that is how the store spells it: one
 * string of paths per tier, which is also how the popover's rows carry theirs.
 *
 * The click is the chip's and not the chrome's. It toggles rather than opens,
 * because the popover has no close button of its own and the chip is the only way
 * back out of it with the pointer.
 */

import { useSyncExternalStore } from 'react'

import * as perm from '../state/perm'

import type { JSX } from 'react'

/** The shield page.html serves, which is also every tier's outline. */
const SERVED = '<path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/>'

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
      <svg
        className="pico"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        aria-hidden="true"
        dangerouslySetInnerHTML={{ __html: p ? p.ico : SERVED }}
      />
      <span id="permName">{p ? p.label : 'Smart mode'}</span>
    </button>
  )
}
