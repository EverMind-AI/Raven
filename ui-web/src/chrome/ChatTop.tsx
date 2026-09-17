/* The chat column's chrome above the dock: the session header, the scroller's
 * three grounds, the back-to-bottom pill's glyph and the wordmark -- four
 * regions src/page.html used to carry as markup.
 *
 * One file per region of the page, under src/chrome/, beside src/features/
 * (islands) and src/shell/ (behaviour modules). Every element below is a
 * transcription -- tag, id, class, data-*, role, aria, the svg path data and
 * the text exactly as page.html spelled them, attributes in the same order --
 * and src/test/__golden__/region-app.txt is what says so. The containers stay
 * in page.html until the end of stage C and these portal into them, one portal
 * per container, so the order React commits is the order written here (see
 * src/App.tsx for why the root is detached). div.dock is the fifth child of
 * .chat and is still page.html's, which is why this cannot be one portal of
 * five children into .chat: a portal appends, so the dock would end up above
 * the header.
 *
 * What this does NOT own, though it renders the elements:
 *   - h1#title's text. Seven modules write it -- features/rail/source.ts and
 *     store.ts when a row is renamed, state/session/registry.ts and runtime.ts
 *     as a conversation opens, loads and streams -- and features/rail/store.ts
 *     swaps the whole heading for an input while the reader renames it. The
 *     literal here is what the page is served with and this never changes it,
 *     so React never writes it again: it diffs against the props it rendered
 *     last rather than against the document.
 *   - #wsBtn's aria-expanded, tooltip and label, and #wsBdg's count and hidden
 *     (legacy/demo/100-workspace.js's setWs and bumpWs).
 *   - #backpill's hidden, tooltip and label, and its click
 *     (features/composer/store.ts's pillPaint, features/composer/mount.tsx) --
 *     all on the container, which is static markup.
 *   - #flash and #stage's children. #stage is shared ground: the transcript
 *     island appends a lane host into it and the composer appends the live
 *     turn's, each keeping its position by identity, so React owning that child
 *     list would tear both out.
 *   - #wsGrip, which has no interior at all, so nothing portals into it.
 */

import { useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { Banner } from './Banner'
import { renameTitle } from '../legacy/demo/050-rail.js'
import * as lang from '../state/lang'

import type { JSX } from 'react'

/* The session header. Its two buttons say their words through data-i18n-tip /
   -aria, which state/lang.ts writes onto the attributes, so there is no literal
   here to take through lang.text -- and #title has no key at all, because its
   text is a conversation's name rather than a phrase from the catalogue. */
function Header(): JSX.Element {
  return (
    <>
      <h1 id="title">新任务</h1>
      <button
        className="ghost-ic tipdn"
        id="renameBtn"
        data-i18n-tip="gui.rename_session"
        data-i18n-aria="gui.rename_session"
        onClick={() => renameTitle()}
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" aria-hidden="true">
          <path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3z" /><path d="M14.5 6.5 17.5 9.5" />
        </svg>
      </button>
      <span className="spacer" />
      <button
        className="ghost-ic wstog tipdn"
        id="wsBtn"
        aria-expanded="false"
        data-i18n-tip="gui.expand_ws"
        data-i18n-aria="gui.expand_ws"
      >
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" /><path d="M14.5 4.5v15" />
        </svg>
        <span className="bdg" id="wsBdg" hidden>2</span>
      </button>
    </>
  )
}

/* The scroller's three grounds. #bannerHost is the one this root fills, so the
   notice is composed here rather than built by hand; the other two are handed
   over empty. */
function Scroll(): JSX.Element {
  return (
    <>
      <div id="bannerHost"><Banner /></div>
      <div className="flash" id="flash" />
      <div className="col" id="stage" />
    </>
  )
}

/* Each interior into the container page.html still provides, guarded the way
   the island mounts are: a document without the container renders nothing
   rather than throwing.
 *
 * The language subscription is the region's, not each component's: the four
 * interiors carry keys the document pass rewrites, and this is what will draw
 * them from the catalogue once that pass is gone. It is also what makes the
 * agreement measurable today -- a flip re-renders all four, and the values the
 * writers above put on these elements have to survive that.
 */
export function ChatTop(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  const top = document.querySelector('.chat > .top')
  const scroll = document.getElementById('scroll')
  const pill = document.getElementById('backpill')
  const brand = document.getElementById('brand')
  return (
    <>
      {top ? createPortal(<Header />, top) : null}
      {scroll ? createPortal(<Scroll />, scroll) : null}
      {pill
        ? createPortal(
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5.5v13M6.5 12.5l5.5 5.5 5.5-5.5" /></svg>,
            pill
          )
        : null}
      {brand
        ? createPortal(
            <>
              {/* Empty until there is artwork to put in it: #brand .mk:empty
                  hides the slot, so the emptiness is what the rule reads. */}
              <span className="mk" />
              <span className="wl">Raven Agent</span>
            </>,
            brand
          )
        : null}
    </>
  )
}
