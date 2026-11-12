/* The permission popover: the three tiers, as a radio group off the chip.
 *
 * Each row is a tile with a ground of its own: the tier's glyph in a plate, the
 * tier's name, and the sentence that explains it, with the chosen tile lifting
 * to a warm wash and carrying the tick. The heading names the list and the note
 * says when a pick takes effect; the chip already says which tier is in force,
 * so neither repeats it.
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
 *
 * The row names its two parts to a screen reader: the name is the label, and
 * the sentence below it is the description. Without the label the sentence is
 * part of the name, so it is read twice.
 *
 * The heading names both the dialog and the radio group the rows form. It is a
 * live element rather than an `aria-label`, so a reader who has not picked a
 * language still gets a name, and the words are announced once.
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
      aria-labelledby="perm-pop-title"
    >
      {/* Only while it stands: a closed popover has no rows, so nothing under
          it can take a click, and the served tree carries the shell alone. */}
      {s.open && s.listed
        ? (
          <>
            <div className="hd"><span className="lab" id="perm-pop-title">{t('gui.perm.title')}</span></div>
            <div role="radiogroup" aria-labelledby="perm-pop-title">
              {s.listed.map((row) => (
                <button
                  key={row.id}
                  className={`prow${row.risk ? ' risk' : ''}`}
                  role="radio"
                  aria-checked={row.ticked ? 'true' : 'false'}
                  aria-labelledby={`perm-nm-${row.id}`}
                  aria-describedby={`perm-sub-${row.id}`}
                  onClick={() => perm.pick(row.id)}
                >
                  <span className="chrome-perm-badge">
                    <PermGlyph id={row.id} size={18} stroke={1.5} className="pico" />
                  </span>
                  <span className="chrome-perm-text">
                    <span className="nm" id={`perm-nm-${row.id}`}>{row.name}</span>
                    <span className="sub" id={`perm-sub-${row.id}`}>{row.sub}</span>
                  </span>
                  {row.ticked ? (
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true" className="tick">
                      <path d={perm.CHECK} />
                    </svg>
                  ) : <span className="chrome-tick-slot" aria-hidden="true" />}
                </button>
              ))}
            </div>
            <p className="chrome-perm-note">{t('gui.perm.note')}</p>
          </>
        )
        : null}
    </div>
  )
}
