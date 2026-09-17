/* The sub-agent effort panel: one row per rung the server offers.
 *
 * Where it renders, when it moves to the body, why the rows go in through a
 * portal into their own list and why they are the list the open took are all as
 * src/chrome/PermPop.tsx records them; the two panels are built the same way on
 * purpose.
 *
 * What differs is the wording. The heading and the note are written on open by
 * state/tier.ts, from the catalogue that answered, and neither carries a
 * data-i18n key: the built-in ladder is a Session Tier and reaches sub-agents,
 * a deployment's own catalogue is a Session Mode and does not, so a language
 * flip walking the document's keys would paint the tier wording back over a
 * mode catalogue's. Rendered empty here, and left alone on every re-render.
 */

import { useLayoutEffect, useRef, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { place } from '../lib/popover'
import * as tier from '../state/tier'

import type { JSX } from 'react'

export function TierPop(): JSX.Element {
  const s = useSyncExternalStore(tier.subscribe, tier.get)
  const box = useRef<HTMLDivElement>(null)
  const list = document.getElementById('tierList')

  useLayoutEffect(() => {
    const pop = box.current
    const chip = document.getElementById('tierChip')
    if (!s.open || !pop || !chip) return
    place(pop, chip)
  }, [s.open, s.opened])

  return (
    <div
      className="pop"
      id="tierPop"
      data-open={s.open ? 'true' : 'false'}
      role="dialog"
      aria-labelledby="tierPopLab"
      ref={box}
    >
      <div className="hd"><span className="lab" id="tierPopLab" /></div>
      <div id="tierList" role="radiogroup" />
      {s.listed && list
        ? createPortal(
          s.listed.map((row) => (
            <button
              key={row.id}
              className="prow"
              role="radio"
              aria-checked={row.ticked ? 'true' : 'false'}
              title={row.sub}
              onClick={() => tier.pick(row.id)}
            >
              <span className="pic">
                <svg viewBox="0 0 24 24" aria-hidden="true" dangerouslySetInnerHTML={{ __html: row.ico }} />
              </span>
              <span className="txt">
                <span className="nm">{row.name}</span>
                {row.sub === undefined ? null : <span className="sub">{row.sub}</span>}
              </span>
              {row.ticked ? (
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true" className="tick">
                  <path d={tier.CHECK} />
                </svg>
              ) : null}
            </button>
          )),
          list
        )
        : null}
      <div className="note" />
    </div>
  )
}
