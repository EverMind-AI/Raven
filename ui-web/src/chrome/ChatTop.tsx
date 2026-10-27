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
 * here too now: src/App.tsx renders div.chat and this is the first seven of its
 * eight children, with the composer dock (src/chrome/Dock.tsx) eighth.
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
 *   - #backpill's and #toppill's hidden, tooltip and label, and their clicks
 *     (features/composer/store.ts's pillPaint, features/composer/mount.tsx).
 *   - #flash and #stage's children. #stage is shared ground: the transcript
 *     island appends a lane host into it and the composer appends the live
 *     turn's, each keeping its position by identity, so React owning that child
 *     list would tear both out.
 *   - #wsGrip, which has no interior at all, so nothing portals into it.
 *   - #turnNav's interior: the transcript island's turn nav roots itself
 *     there (features/transcript/TurnNav.tsx).
 */

import { MoreVerticalIcon } from '@hugeicons/core-free-icons'
import { useSyncExternalStore } from 'react'

import { Icon } from '../components/Icon'
import { RavenMark } from '../components/RavenMark'
import * as exportWizard from '../features/export/store'
import { rename as renameSession } from '../features/rail/store'
import * as trajectory from '../features/trajectory/store'
import { t } from '../i18n/t'
import { readingImported } from '../lib/imported'
import { servesBugReport } from '../rpc/capabilities'
import * as lang from '../state/lang'
import { show as showMenu } from '../state/menu'
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
      <h1 id="title">New task</h1>
      {/* The folder this conversation runs in, said once beside its name: the
          composer's workspace chip is gone once a conversation starts
          (src/chrome/WorkdirTag.tsx). */}
      <WorkdirTag />
      <span className="spacer" />
      <TrajectoryToggle />
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
      <MoreButton />
    </>
  )
}

/* The conversation's menu, in the corner slot: always rendered, so the slot
   beside it -- the panel toggle's, where the desk launcher stands -- never
   moves with the conversation, and disabled while there is nothing the menu
   could act on. Whether there is reads the trajectory store, as the toggle
   beside it does, through the export domain's own predicate. The rows are the
   shared context menu's, placed under the button: Export always, and Report a
   problem while the gateway announces a bug report receiver. */
function MoreButton(): JSX.Element {
  const state = useSyncExternalStore(trajectory.subscribe, trajectory.get)
  const key = state.sessionKey
  const offered = exportWizard.exportable(key, trajectory.available(state))
  return (
    <button
      className="ghost-ic tipdn"
      id="moreBtn"
      aria-haspopup="menu"
      data-tip={lang.attr('gui.export.more')}
      aria-label={lang.attr('gui.export.more')}
      disabled={!offered}
      onClick={(e) => {
        if (!offered || key === null) return
        e.stopPropagation()
        const r = e.currentTarget.getBoundingClientRect()
        showMenu(r.left, r.bottom + 4, [
          { label: t('gui.export.menu_export'), fn: () => exportWizard.open('export', key) },
          ...(servesBugReport()
            ? [{ label: t('gui.export.menu_report'), fn: () => exportWizard.open('report', key) }]
            : []),
        ])
      }}
    >
      <Icon icon={MoreVerticalIcon} size={15} stroke={1.8} />
    </button>
  )
}

/* The switch between the conversation and its trajectory, left of the panel
   toggle: the corner slot is the conversation menu's (`#moreBtn`), and the
   desk launcher stands in the panel toggle's slot beside it (fixed one slot in
   from the window's edge, `#wsBtn` hidden under `.desk-ready`), so anything
   placed between the two is covered the moment a desk is up. Rendered only while the trajectory store says the view may be
   offered -- the gateway serves it, it is on, a conversation with content is
   open -- so the served header carries no trace of it, and the words come
   through the catalogue because they change with the view. The store is read
   through its own subscription rather than the region's language one: a flip
   of the view is not a flip of the language. */
function TrajectoryToggle(): JSX.Element | null {
  const state = useSyncExternalStore(trajectory.subscribe, trajectory.get)
  if (!trajectory.available(state)) return null
  const on = state.view === 'trajectory'
  const label = t(on ? 'gui.trajectory.back' : 'gui.trajectory.show')
  /* The icon names where the click goes: the trajectory's line while the
     conversation is up, the conversation's bubble while the trajectory is. */
  return (
    <button
      className="ghost-ic tipdn chrome-traj"
      id="trajBtn"
      aria-pressed={on}
      data-tip={label}
      aria-label={label}
      onClick={trajectory.toggleView}
    >
      {on ? (
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M4.5 5.5h15v10.5h-9.5l-5.5 4v-4h0z" />
        </svg>
      ) : (
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M4 17.5h4l3-11 3 11h6" /><circle cx="18" cy="17.5" r="1.5" />
        </svg>
      )}
    </button>
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

/* The first seven children of div.chat, the ones page.html carried in the
 * order it had them; #trajHost and #turnNav are the two boxes added since.
 *
 * The language subscription is the region's, not each component's: the five
 * interiors carry keys and this is what draws them from the catalogue on a
 * pick. It is also what makes the agreement measurable -- a flip re-renders all
 * five, and the values the writers above put on these elements have to survive
 * that.
 */
export function ChatTop(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  /* Which of the column's two views is up. The scroller is parked rather than
     unmounted -- out of the flow, hidden, its scroll position kept -- and the
     trajectory's box takes its place; the attribute is rendered only while the
     trajectory is up, so the served markup is the markup it always was. */
  const parked = useSyncExternalStore(trajectory.subscribe, trajectory.get).view === 'trajectory'
  return (
    <>
      <div
        className="top"
        onClick={(e) => { if ((e.target as Element).closest('#title') && !readingImported()) renameSession() }}
      >
        <Header />
      </div>
      <div className="scroll" id="scroll" data-parked={parked ? '' : undefined}><Scroll /></div>
      {/* The trajectory island's box, handed over empty (features/trajectory/manifest.ts). */}
      <div id="trajHost" hidden={!parked} />
      {/* The other end of the quick-jump pair: the last question above what the
          top edge is showing. .tipdn, because its hover pill belongs below the
          control rather than over the header it sits under. */}
      <button className="backpill tipdn chrome-toppill" id="toppill" hidden>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 18.5v-13M6.5 11.5l5.5-5.5 5.5 5.5" /></svg>
      </button>
      <button className="backpill" id="backpill" hidden>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5.5v13M6.5 12.5l5.5 5.5 5.5-5.5" /></svg>
      </button>
      {/* The turn nav's box, handed over empty (features/transcript/manifest.ts).
          After the scroller, so the stylesheet parks it with the scroller. */}
      <div id="turnNav" />
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
