/* The permission popover: the three tiers, as a radio group off the chip.
 *
 * Each row is a glyph, the tier's name and the sentence that explains it, with
 * the chosen one carrying a warm band and the tick. The heading names the list
 * and the note says when a pick takes effect; the chip already says which tier
 * is in force, so neither repeats it.
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
 * by rendering the list the open took. The heading and the note read the live
 * catalogue, because they name the control rather than one of its rows.
 */

import { useSyncExternalStore } from 'react'

import { t } from '../i18n/t'
import * as lang from '../state/lang'
import * as perm from '../state/perm'
import { PermGlyph } from './PermGlyph'

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
        ? (
          <>
            <div className="hd"><span className="lab">{t('gui.perm.title')}</span></div>
            {s.listed.map((row) => (
              <button
                key={row.id}
                className={`prow${row.risk ? ' risk' : ''}`}
                role="radio"
                aria-checked={row.ticked ? 'true' : 'false'}
                aria-describedby={`perm-sub-${row.id}`}
                onClick={() => perm.pick(row.id)}
              >
                <PermGlyph id={row.id} size={18} stroke={1.5} className="pico" />
                <span className="chrome-perm-text">
                  <span className="nm">{row.name}</span>
                  <span className="sub" id={`perm-sub-${row.id}`}>{row.sub}</span>
                </span>
                {row.ticked ? (
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true" className="tick">
                    <path d={perm.CHECK} />
                  </svg>
                ) : <span className="chrome-tick-slot" aria-hidden="true" />}
              </button>
            ))}
            <p className="chrome-perm-note">{t('gui.perm.note')}</p>
          </>
        )
        : null}
    </div>
  )
}
