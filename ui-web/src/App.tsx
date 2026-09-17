/* The page's own React root: the regions src/page.html used to carry as markup
 * -- the dialog shells, the rail, the composer dock and the capabilities
 * page's chrome -- and the two overlays, the context menu's rows and the
 * notices, that the chrome used to build by hand.
 *
 * Every element below is a transcription -- tag, id, class, data-*, role, aria
 * and text exactly as page.html spelled them, attributes in the same order --
 * and the goldens under src/test/__golden__/ are what says so. A region big
 * enough to read on its own gets a file under src/chrome/ (the rail, the
 * dock, the capabilities page).
 *
 * The containers stay in page.html and the interiors portal into them, which is
 * the mechanism for every region stage C converts: while a container is static
 * markup, the order React commits into it is the order these portals are
 * written, so an interior cannot end up appended after something the page put
 * there first. React clears a container it is given as a ROOT (measured: a root
 * at document.body deletes #splash, #noJs and every static region on its first
 * commit), which is why the root in main.tsx is a detached element and
 * everything here is a portal.
 *
 * Language. The static markup carries data-i18n* keys rather than t() calls,
 * and state/lang.ts applies a language by walking the document and rewriting
 * them. A component that renders one of those keyed literals therefore has two
 * writers, and it takes its literal through lang.text(key, literal) -- the same
 * key, the same catalogue -- so the two agree on every value in both
 * directions. The literals with no key (#cfTitle, #cfYes, #setTitle) have
 * nothing to look up: each is owned by whoever writes it afterwards (the
 * confirm store, the settings island), and a re-render cannot undo that,
 * because React diffs against the props it rendered last rather than against
 * the document.
 *
 * Not here, and not later: #splash and #noJs. Both are pre-JavaScript shells --
 * the splash is the literal first frame, painted while this bundle is still
 * being evaluated, and #noJs is what a reader gets when it never runs -- so
 * neither can be something React puts on screen. They stay in page.html and are
 * taken down at boot (state/splash.ts, legacy/demo/010-kernel.js).
 */
import { useEffect, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { CapsPage } from './chrome/CapsPage'
import { ChatTop } from './chrome/ChatTop'
import { Dock } from './chrome/Dock'
import { Rail } from './chrome/Rail'
import { WsPane } from './chrome/WsPane'
import * as menu from './shell/menu'
import * as toast from './shell/toast'
import * as confirm from './state/confirm'
import * as detail from './state/detail'
import * as lang from './state/lang'
import * as settings from './state/settingsDialog'

import type { JSX } from 'react'

/* A veil's own click, for the three dialogs that close when the reader clicks
   beside them. The veil IS the container, which src/page.html still renders, so
   this cannot be an onClick; the guard is what the legacy handlers had -- the
   panel is a child, so a click inside it is not a click on the scrim. Every
   handler passed in is a module function, so the listener is registered once
   and not on every render. */
function useScrim(id: string, close: () => void): void {
  useEffect(() => {
    const el = document.getElementById(id)
    if (!el) return
    const away = (e: Event): void => {
      if (e.target === el) close()
    }
    el.addEventListener('click', away)
    return () => el.removeEventListener('click', away)
  }, [id, close])
}

/* Cancelling is what the scrim and the Escape chain both do, and the chain does
   it by clicking #cfNo -- which reaches this through the button's onClick. */
const cancel = (): void => confirm.answer(false)

/* The confirm dialog. The question is state/confirm.ts's: the asker hands it a
   title, a body and a label for the yes button, and until something asks, the
   three literals the page was served with stand. */
function ConfirmSheet(): JSX.Element {
  const s = useSyncExternalStore(confirm.subscribe, confirm.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  useScrim('veil', cancel)
  return (
    <div className="sheet" role="dialog" aria-modal="true" aria-labelledby="cfTitle">
      <header id="cfTitle">{s.title ?? '确认'}</header>
      <div className="body" id="cfBody">{s.body}</div>
      <footer>
        <button className="btn" id="cfNo" data-i18n="gui.cancel" onClick={cancel}>{lang.text('gui.cancel', '取消')}</button>
        <button className="btn bad" id="cfYes" onClick={() => confirm.answer(true)}>{s.label ?? '确认'}</button>
      </footer>
    </div>
  )
}

/* The shared detail drawer. #dBody renders empty on purpose: state/detail.ts
   keeps one host per owner under it and each island renders its card into its
   own, so React must not own that child list.

   The title is the store's. It is the served em-dash until a card claims the
   header, and every card blanks it -- an empty <b> is what turns the header row
   into a floating close control (`.detail header:has(b:empty)`, page.css:3592),
   so the emptiness is load-bearing and no card ever puts the dash back. */
function DetailPanel(): JSX.Element {
  const s = useSyncExternalStore(detail.subscribe, detail.get)
  useScrim('detail', detail.close)
  return (
    <div className="dpanel">
      <header>
        <b id="dTitle">{s.title ?? '—'}</b>
        <button className="dx" id="dClose" data-i18n-aria="gui.close" aria-label="关闭" onClick={() => detail.close()}>
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </header>
      <div className="body" id="dBody" />
    </div>
  )
}

/* The settings dialog's frame. #snavList and #spanels render empty for the same
   reason as #dBody: the settings island portals its nav into the first and
   roots its panels in the second.

   #setTitle is the island's: it writes the section's name there on every draw.
   The literal below is what the page is served with, and is why this component
   must not render a value of its own for it. */
function SettingsModal(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  useScrim('setVeil', settings.close)
  return (
    <div className="smodal" id="setModal" role="dialog" aria-modal="true" data-i18n-aria="gui.page.set">
      <nav className="snav" id="snav">
        {/* A block row with one inline child, so the whitespace page.html had
            around it is reproduced: it collapses at both line edges either way,
            but the rule that makes it harmless is the line edge, not a flex or
            grid parent (.snav .brandrow, src/styles/page.css:3667). */}
        <div className="brandrow">
          {' '}
          <span className="wm" data-i18n="gui.page.set">{lang.text('gui.page.set', '设置')}</span>{' '}
        </div>
        <div className="snavlist" id="snavList" />
      </nav>
      <div className="sbody">
        <header className="shd">
          <div className="ttl">
            <h3 id="setTitle">设置</h3>
            <p className="sub" id="setSub" />
          </div>
          <button className="icb" id="setClose" data-i18n-tip="gui.close" data-i18n-aria="gui.close" onClick={() => settings.close()}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
          </button>
        </header>
        <div className="spanels" id="spanels" />
      </div>
    </div>
  )
}

/* The context menu's rows, in the host they were raised in (shell/menu.ts). The
   flag and the position are the store's, because they belong to div#menu, which
   page.html still renders; what is here is the row list it had built by hand. */
function ContextMenu(): JSX.Element | null {
  const s = useSyncExternalStore(menu.subscribe, menu.get)
  if (!s.host) return null
  return createPortal(
    s.items.map((item, i) =>
      item === '-' ? (
        <hr key={i} />
      ) : (
        <button key={i} className={item.bad ? 'bad' : undefined} onClick={() => menu.pick(item)}>{item.label}</button>
      )
    ),
    s.host
  )
}

/* One notice (shell/toast.ts). A notice offering an action carries the button
   that takes it; a plain one is one span, and the difference is what the two
   lifetimes are for. */
function Notice({ t }: { t: toast.Toast }): JSX.Element {
  return (
    <div className="toast">
      <span className="t">{t.text}</span>
      {t.action ? <button onClick={() => toast.run(t.id)}>{t.action.label}</button> : null}
    </div>
  )
}

/* Each notice into the host it was raised in, oldest first -- a host is
   captured when the notice is raised, the way the writer resolved #toasts per
   call, so a page that replaced the host cannot move a notice already up. */
function Toasts(): JSX.Element {
  const live = useSyncExternalStore(toast.subscribe, toast.get)
  return <>{live.map((t) => createPortal(<Notice t={t} />, t.host, String(t.id)))}</>
}

/* Each interior into the container page.html still provides. Guarded the way
   the island mounts are: a document without the container renders nothing
   rather than throwing. The rail, the chat column and the workspace pane
   resolve their own containers -- div.app's two columns are the regions with no
   id -- so does the dock, which is nested inside one, and the two overlays
   below find their own host, because theirs is the one that was standing when
   they were raised. */
export function App(): JSX.Element {
  const veilEl = document.getElementById('veil')
  const detailEl = document.getElementById('detail')
  const setVeilEl = document.getElementById('setVeil')
  return (
    <>
      <Rail />
      <ChatTop />
      <Dock />
      <WsPane />
      <CapsPage />
      {detailEl ? createPortal(<DetailPanel />, detailEl) : null}
      {setVeilEl ? createPortal(<SettingsModal />, setVeilEl) : null}
      {veilEl ? createPortal(<ConfirmSheet />, veilEl) : null}
      <ContextMenu />
      <Toasts />
    </>
  )
}
