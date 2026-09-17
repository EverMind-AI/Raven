/* The permission panel: the three tiers, as a radio group over the composer.
 *
 * WHERE this renders is the one thing it does not decide. The panel is a child
 * of the composer card here, which is where the page is served with it, and
 * shell/perm.ts's open moves the node to the body the first time it opens --
 * once, and never back: the card's entrance animation makes the card a
 * containing block, which re-bases the panel's position: fixed against the card
 * instead of the viewport. React renders on into a child it no longer holds,
 * because none of the card's children is conditional and it therefore never
 * reconciles that child list (src/chrome/Dock.tsx).
 *
 * The rows go in through a portal into #permList, though #permList is rendered
 * right here, and that is what makes them clickable: React delegates a click
 * from the container it rendered a tree into, and by the time there are rows to
 * click the panel has left that container for the body. Measured -- a row's
 * onClick simply stops firing. A portal makes the list its own container, and
 * the list travels with the panel.
 *
 * The rows themselves are the store's, words and tick and all, read when the
 * panel opened rather than while it stands: a language applied over an open
 * panel left the rows in the language they were built in, and a pick left the
 * tick on the tier that was in force when they were built. Both are reproduced
 * by rendering the list the open took.
 *
 * The heading and the note carry keys, so each takes its literal through
 * lang.text: the lang store rewrites the two by hand over the document, and a
 * component rendering the same key has to agree with it in both directions.
 */

import { useLayoutEffect, useRef, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import * as perm from '../shell/perm'
import { place } from '../shell/popover'
import * as lang from '../state/lang'

import type { JSX } from 'react'

export function PermPop(): JSX.Element {
  const s = useSyncExternalStore(perm.subscribe, perm.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const box = useRef<HTMLDivElement>(null)
  /* Resolved while rendering, the way the dock resolves its own band: the list
     is this component's first render, so there is nothing to portal into until
     after it -- and nothing to portal either, since a panel that has never
     been opened has no rows. */
  const list = document.getElementById('permList')

  /* After the commit that filled and showed the panel, and once per open: a
     panel is measured where it stands, at the size the rows just gave it. */
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
      ref={box}
    >
      <div className="hd"><span className="lab" data-i18n="gui.perm.title">{lang.text('gui.perm.title', '权限模式')}</span></div>
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
      <div className="note" data-i18n="gui.perm.note">{lang.text('gui.perm.note', '下一个工具调用起生效。')}</div>
    </div>
  )
}
