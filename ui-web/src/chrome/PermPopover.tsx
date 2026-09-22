/* The permission popover: the three tiers, as a radio group off the chip.
 *
 * Names only. Each row says its tier and carries the sentence that explains it
 * on the title, because a reader who has picked once knows the three words and
 * the one who has not hovers. No heading and no note: the chip the popover
 * hangs off already says what is being chosen.
 *
 * WHERE this renders is the one thing it does not decide. It is a child of the
 * chip's anchor (`.chrome-anch`, src/chrome/Dock.tsx), hung off the chip's top edge by
 * the stylesheet, and never moved -- which is what lets every row carry a plain
 * onClick: the tree never leaves the container React delegates from.
 *
 * The rows themselves are the store's, words and tick and all, read when the
 * popover opened rather than while it stands: a language applied over an open
 * popover left the rows in the language they were built in, and a pick left the
 * tick on the tier that was in force when they were built. Both are reproduced
 * by rendering the list the open took.
 */

import { useSyncExternalStore } from 'react'

import * as lang from '../state/lang'
import * as perm from '../state/perm'

import type { JSX } from 'react'

export function PermPopover(): JSX.Element {
  const s = useSyncExternalStore(perm.subscribe, perm.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div
      className="pop"
      id="permPop"
      data-open={s.open ? 'true' : 'false'}
      role="dialog"
      aria-label={lang.attr('gui.perm.title')}
    >
      {/* Only while it stands: a closed popover has no rows, so nothing under
          it can take a click, and the served tree carries the shell alone. */}
      {s.open && s.listed
        ? s.listed.map((row) => (
          <button
            key={row.id}
            className={`prow${row.risk ? ' risk' : ''}`}
            role="radio"
            aria-checked={row.ticked ? 'true' : 'false'}
            title={row.sub}
            onClick={() => perm.pick(row.id)}
          >
            <span className="nm">{row.name}</span>
            {row.ticked ? (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true" className="tick">
                <path d={perm.CHECK} />
              </svg>
            ) : null}
          </button>
        ))
        : null}
    </div>
  )
}
