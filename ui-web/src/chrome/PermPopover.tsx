/* The permission popover: the three tiers, as a radio group over the composer.
 *
 * WHERE this renders is the one thing it does not decide. The popover is a child
 * of the composer card here, which is where the page is served with it, and
 * state/perm.ts's open moves the node to the body the first time it opens --
 * once, and never back: the card's entrance animation makes the card a
 * containing block, which re-bases the popover's position: fixed against the card
 * instead of the viewport. React renders on into a child it no longer holds,
 * because none of the card's children is conditional and it therefore never
 * reconciles that child list (src/chrome/Dock.tsx).
 *
 * The rows go in through a portal into #permList, though #permList is rendered
 * right here, and that is what makes them clickable: React delegates a click
 * from the container it rendered a tree into, and by the time there are rows to
 * click the popover has left that container for the body. Measured -- a row's
 * onClick simply stops firing. A portal makes the list its own container, and
 * the list travels with the popover.
 *
 * The rows themselves are the store's, words and tick and all, read when the
 * popover opened rather than while it stands: a language applied over an open
 * popover left the rows in the language they were built in, and a pick left the
 * tick on the tier that was in force when they were built. Both are reproduced
 * by rendering the list the open took.
 *
 * The heading and the note carry keys, so each reads the catalogue as it
 * renders (t(key)), in whichever language the page resolved.
 */

import { useLayoutEffect, useRef, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { place } from '../lib/popover'
import { t } from '../i18n/t'
import * as lang from '../state/lang'
import * as perm from '../state/perm'

import type { JSX } from 'react'

export function PermPopover(): JSX.Element {
  const s = useSyncExternalStore(perm.subscribe, perm.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const box = useRef<HTMLDivElement>(null)
  /* Resolved while rendering, the way the dock resolves its own band: the list
     is this component's first render, so there is nothing to portal into until
     after it -- and nothing to portal either, since a popover that has never
     been opened has no rows. */
  const list = document.getElementById('permList')

  /* After the commit that filled and showed the popover, and once per open: a
     popover is measured where it stands, at the size the rows just gave it. */
  useLayoutEffect(() => {
    const pop = box.current
    const chip = document.getElementById('permChip')
    if (!s.open || !pop || !chip) return
    place(pop, chip)
  }, [s.open, s.opened])

  return (
    <div
      className="pop"
      id="permPop"
      data-open={s.open ? 'true' : 'false'}
      role="dialog"
      data-i18n-aria="gui.perm.title"
      aria-label={lang.attr('gui.perm.title')}
      ref={box}
    >
      <div className="hd"><span className="lab" data-i18n="gui.perm.title">{t('gui.perm.title')}</span></div>
      <div id="permList" />
      {s.listed && list
        ? createPortal(
          s.listed.map((row) => (
            <button
              key={row.id}
              className={`prow${row.risk ? ' risk' : ''}`}
              role="radio"
              aria-checked={row.ticked ? 'true' : 'false'}
              onClick={() => perm.pick(row.id)}
            >
              <span className="pic">
                <svg viewBox="0 0 24 24" aria-hidden="true" dangerouslySetInnerHTML={{ __html: row.ico }} />
              </span>
              <span className="txt"><span className="nm">{row.name}</span><span className="sub">{row.sub}</span></span>
              {row.ticked ? (
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true" className="tick">
                  <path d={perm.CHECK} />
                </svg>
              ) : null}
            </button>
          )),
          list
        )
        : null}
      <div className="note" data-i18n="gui.perm.note">{t('gui.perm.note')}</div>
    </div>
  )
}
