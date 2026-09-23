/* The chat column's chrome above the dock: the session header, the scroller's
 * three grounds, the back-to-bottom pill's glyph and the wordmark -- four
 * regions src/page.html used to carry as markup.
 *
 * One file per region of the page, under src/chrome/, beside src/features/
 * (islands), src/state/ and src/chrome/behaviour/ (the modules that own
 * listeners and measurements rather than markup). Every element below
 * is a transcription -- tag, id, class, data-*, role, aria, the svg path data and
 * the text exactly as page.html spelled them, attributes in the same order --
 * and src/test/__golden__/region-app.txt is what says so. The containers are
 * here too now: src/App.tsx renders div.chat and this is the first five of its
 * six children, with the composer dock (src/chrome/Dock.tsx) sixth.
 *
 * What this does NOT own, though it renders the elements:
 *   - h1#title's text. Seven modules write it -- features/rail/source.ts and
 *     store.ts when a row is renamed, state/session/registry.ts and runtime.ts
 *     as a conversation opens, loads and streams -- and features/rail/store.ts
 *     swaps the whole heading for an input while the reader renames it, which
 *     a click on the heading starts. The literal here is what the page is
 *     served with and this never changes it, so React never writes it again:
 *     it diffs against the props it rendered last rather than against the
 *     document.
 *   - #wsBtn's aria-expanded, and #wsBdg's count and hidden (state/ws.ts's
 *     setOpen and bump). Its tooltip and label have two writers, which is the
 *     behaviour: state/ws.ts names them for the state the pane is in, and the
 *     keyed value below is written on a language pick -- so a pick puts the
 *     word for "expand" back on an open pane's toggle, exactly as the document
 *     pass did.
 *   - #backpill's hidden, tooltip and label, and its click
 *     (features/composer/store.ts's pillPaint, features/composer/mount.tsx).
 *   - #flash and #stage's children. #stage is shared ground: the transcript
 *     island appends a lane host into it and the composer appends the live
 *     turn's, each keeping its position by identity, so React owning that child
 *     list would tear both out.
 *   - #wsGrip, which has no interior at all, so nothing portals into it.
 */

import { useSyncExternalStore } from 'react'

import { RavenMark } from '../components/RavenMark'
import { rename as renameSession } from '../features/rail/store'
import { t } from '../i18n/t'
import * as lang from '../state/lang'
import { Banner } from './Banner'
import { WorkdirTag } from './WorkdirTag'

import type { JSX } from 'react'

/* The session header. The panel toggle says its words in a tooltip and a
   label rather than in text, so it takes its key through lang.attr -- which is
   absent until a pick lands, the way the served markup carried neither -- and
   #title has no key at all, because its text is a conversation's name rather
   than a phrase from the catalogue. The workspace tag beside it renders the
   whole of state/workdir.ts's paint.

   The name is edited by clicking it: no pencil, the heading itself is the
   control, and the stylesheet gives it a text cursor and a frame on hover. The
   click is taken on the row rather than on the heading, because the rename
   swaps the heading for an input and puts a fresh heading back
   (features/rail/store.ts), and a handler on the node React rendered would
   be gone with it after the first edit. */
function Header(): JSX.Element {
  return (
    <>
      <h1 id="title">新任务</h1>
      {/* The folder this conversation runs in, said once beside its name: the
          composer's workspace chip is gone once a conversation starts
          (src/chrome/WorkdirTag.tsx). */}
      <WorkdirTag />
      <span className="spacer" />
      <button
        className="ghost-ic wstog tipdn"
        id="wsBtn"
        aria-expanded="false"
        data-tip={lang.attr('gui.expand_ws')}
        aria-label={lang.attr('gui.expand_ws')}
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

/* The first five children of div.chat, in the order page.html had them.
 *
 * The language subscription is the region's, not each component's: the five
 * interiors carry keys and this is what draws them from the catalogue on a
 * pick. It is also what makes the agreement measurable -- a flip re-renders all
 * five, and the values the writers above put on these elements have to survive
 * that.
 */
export function ChatTop(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <>
      <div
        className="top"
        onClick={(e) => { if ((e.target as Element).closest('#title')) renameSession() }}
      >
        <Header />
      </div>
      <div className="scroll" id="scroll"><Scroll /></div>
      <button className="backpill" id="backpill" hidden>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5.5v13M6.5 12.5l5.5 5.5 5.5-5.5" /></svg>
      </button>
      {/* The workspace seam, hung off the chat rather than the panel: the panel
          clips its own overflow, so a grip inside it could only be grabbed from
          one side. No interior at all, and dragged by id from chrome/behaviour/panes.ts. */}
      <div
        className="grip"
        id="wsGrip"
        role="separator"
        aria-orientation="vertical"
        title={lang.attr('gui.resize_ws')}
        aria-label={lang.attr('gui.resize_ws')}
      />
      <div id="brand" aria-hidden="true">
        <span className="mk"><RavenMark size={52} /></span>
        <span className="wl">{t('gui.brand.hi')}</span>
      </div>
    </>
  )
}
