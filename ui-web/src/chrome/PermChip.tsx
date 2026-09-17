/* The permission chip in the bar under the field, and the click that opens its
 * panel.
 *
 * The chip's own state -- the tier's icon, its name, the warning class and the
 * accessible name -- is painted by state/perm.ts's draw over what this renders:
 * four writes by id whose values this component never renders one of, so React
 * cannot undo them (see that module). What is rendered here is the shape the
 * page is served with, plus the one attribute that belongs to the panel rather
 * than to the tier: aria-expanded, which is the store's.
 *
 * The click is the chip's and not the chrome's. It toggles rather than opens,
 * because the panel has no close button of its own and the chip is the only way
 * back out of it with the pointer.
 */

import { useSyncExternalStore } from 'react'

import * as perm from '../state/perm'

import type { JSX } from 'react'

export function PermChip(): JSX.Element {
  const s = useSyncExternalStore(perm.subscribe, perm.get)
  return (
    <button
      className="chip"
      id="permChip"
      aria-expanded={s.open ? 'true' : 'false'}
      aria-haspopup="true"
      onClick={() => perm.toggle()}
    >
      <svg className="pico" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z" />
      </svg>
      <span id="permName">自动执行</span>
    </button>
  )
}
