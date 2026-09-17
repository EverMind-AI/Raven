/* The page. Every region src/page.html used to carry as markup is rendered
 * from here: the shell's two columns, the seven module pages, the dialog
 * shells, the context menu's rows, the notices, the interiors of the sheets
 * that dock above the composer, and the four layers that belong to no page at
 * all.
 *
 * Every element below is a transcription -- tag, id, class, data-*, role, aria
 * and text exactly as page.html spelled them, attributes in the same order --
 * and the goldens under src/test/__golden__/ are what says so. A region big
 * enough to read on its own gets a file under src/chrome/: the rail, the chat
 * column, the composer dock, the workspace pane, the capabilities page.
 *
 * ONE portal, at the body, and the whole tree inside it. React clears a
 * container it is given as a ROOT -- measured: a root at document.body deletes
 * everything the document was served with on its first commit -- so the root in
 * main.tsx is a detached element and this is a portal into the body it renders.
 * A portal appends, which is what puts these regions after the two shells
 * page.html still carries and before the four runtime layers that are appended
 * during install; state/portals.ts is where that body-level order is declared,
 * because two steps of the `--z` ladder are ties decided by it alone.
 *
 * Language. Each region renders its words through lang.text(key, literal) and
 * its keyed attributes through lang.attr(key), which is applyI18n's five passes
 * read from the other end: the literal the page was served with until a pick
 * lands, the catalogue's text afterwards. The data-i18n* keys stay on the
 * elements -- they are what says which phrase a line of chrome speaks, and the
 * region goldens record them -- but nothing reads them any more (see
 * state/lang.ts). The literals with no key (#cfTitle, #cfYes, #setTitle,
 * #title) have nothing to look up: each is owned by whoever writes it
 * afterwards, and a re-render cannot undo that, because React diffs against the
 * props it rendered last rather than against the document.
 *
 * Flags this renders but does not own, for the same reason: `data-open` on the
 * seven pages and the veils, `hidden` on button#railShow, `data-rail` and
 * `data-page` on div.app, `data-open` / `data-full` on #split. Each is rendered
 * as the value the page is served with and written afterwards by the one store
 * that owns it (state/page.ts, state/rail.ts, state/ws.ts) -- which is also
 * what keeps the order those writes land in.
 *
 * Not here, and not later: #splash and #noJs. Both are pre-JavaScript shells --
 * the splash is the literal first frame, painted while this bundle is still
 * being evaluated, and #noJs is what a reader gets when it never runs -- so
 * neither can be something React puts on screen. They stay in page.html and are
 * taken down at boot (app/splash.ts). #onb stays
 * with them because a portal can only append: rendered from here it would land
 * after #noJs instead of between the two.
 */
import { useEffect, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { CapsPage } from './chrome/CapsPage'
import { ChatTop } from './chrome/ChatTop'
import { Dock } from './chrome/Dock'
import { FailureBars } from './chrome/FailureBar'
import { Lightbox } from './chrome/Lightbox'
import { Rail } from './chrome/Rail'
import { SheetRack } from './chrome/SheetRack'
import { Tooltip } from './chrome/Tooltip'
import { UpgradeShade } from './chrome/UpgradeShade'
import { WsPane } from './chrome/WsPane'
import * as confirm from './state/confirm'
import * as detail from './state/detail'
import * as lang from './state/lang'
import * as menu from './state/menu'
import * as rail from './state/rail'
import * as settings from './state/settingsDialog'
import * as toast from './state/toast'

import type { JSX } from 'react'

/* A veil's own click, for the three dialogs that close when the reader clicks
   beside them. The veil IS the region, and the panel inside it is a child, so
   a click on the scrim is a click on the veil itself -- which is the guard the
   legacy handlers had. A listener rather than an onClick because every handler
   passed in is a module function, so it is registered once and not on every
   render. */
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
    <div className="veil" id="veil" data-open="false">
      <div className="sheet" role="dialog" aria-modal="true" aria-labelledby="cfTitle">
        <header id="cfTitle">{s.title ?? '确认'}</header>
        <div className="body" id="cfBody">{s.body}</div>
        <footer>
          <button className="btn" id="cfNo" data-i18n="gui.cancel" onClick={cancel}>{lang.text('gui.cancel', '取消')}</button>
          <button className="btn bad" id="cfYes" onClick={() => confirm.answer(true)}>{s.label ?? '确认'}</button>
        </footer>
      </div>
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
  useSyncExternalStore(lang.subscribe, lang.get)
  useScrim('detail', detail.close)
  return (
    <aside
      className="detail"
      id="detail"
      data-open="false"
      role="dialog"
      aria-modal="true"
      data-i18n-aria="gui.cap_detail"
      aria-label={lang.attr('gui.cap_detail')}
    >
      <div className="dpanel">
        <header>
          <b id="dTitle">{s.title ?? '—'}</b>
          <button className="dx" id="dClose" data-i18n-aria="gui.close" aria-label={lang.text('gui.close', '关闭')} onClick={() => detail.close()}>
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
          </button>
        </header>
        <div className="body" id="dBody" />
      </div>
    </aside>
  )
}

/* The settings dialog. A dialog, not a place: it is something you adjust and
   dismiss, and taking over the whole window made a two-second change feel like
   leaving the session behind.
 *
 * #snavList and #spanels render empty for the same reason as #dBody: the
 * settings island portals its nav into the first and roots its panels in the
 * second.
 *
 * #setTitle is the island's: it writes the section's name there on every draw.
 * The literal below is what the page is served with, and is why this component
 * must not render a value of its own for it. */
function SettingsModal(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  useScrim('setVeil', settings.close)
  return (
    <div className="veil setveil" id="setVeil" data-open="false">
      <div className="smodal" id="setModal" role="dialog" aria-modal="true" data-i18n-aria="gui.page.set" aria-label={lang.attr('gui.page.set')}>
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
            <button
              className="icb"
              id="setClose"
              data-i18n-tip="gui.close"
              data-i18n-aria="gui.close"
              data-tip={lang.attr('gui.close')}
              aria-label={lang.attr('gui.close')}
              onClick={() => settings.close()}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
            </button>
          </header>
          <div className="spanels" id="spanels" />
        </div>
      </div>
    </div>
  )
}

/* The context menu's rows, in the host they were raised in (state/menu.ts). The
   flag and the position are the store's, because they belong to div#menu, which
   this file renders as an empty region; what is here is the row list the writer
   used to build by hand. */
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

/* One notice (state/toast.ts). A notice offering an action carries the button
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

/* The way back when the rail is collapsed. It sits out here rather than in the
   chat header because that header opens a stacking context of its own (.top is
   positioned with a z-index), which capped this button below the full-page
   modules no matter how high its own z-index went -- collapsing the rail inside
   技能 / 插件 / 记忆 then covered the only control that brings it back, with no
   way left to reach another module.

   Collapsing the rail is the reader's call, never the window's: it holds the
   session list, and having it vanish on resize loses your place. This is the
   twin that brings it back; `hidden` is state/rail.ts's, and it shows exactly
   while the rail does not. */
function RailShow(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <button
      className="ghost-ic tipdn"
      id="railShow"
      data-i18n-tip="gui.expand_rail"
      data-i18n-aria="gui.expand_rail"
      hidden
      data-tip={lang.attr('gui.expand_rail')}
      aria-label={lang.attr('gui.expand_rail')}
      onClick={() => rail.set(true)}
    >
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" /><path d="M9.5 4.5v15" />
      </svg>
    </button>
  )
}

/* One of the six module pages whose whole interior is a heading and the empty
   box its island roots itself in. The seventh, the capabilities page, serves two
   modules and has a file of its own (src/chrome/CapsPage.tsx).

   The heading is drawn and then hidden (`.page > header h2{display:none}`,
   src/styles/page.css): the strip stays for breathing room and the scroll fade,
   and each page's own hero says the name bigger. It is still the page's
   accessible name through the aria-label above, which is why the two keys can
   differ (the memory page is announced by its hero's phrase). */
function ModulePage({ id, aria, head, literal, body }: {
  readonly id: string
  readonly aria: string
  readonly head: string
  readonly literal: string
  readonly body: string
}): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <section className="page" id={id} data-open="false" data-i18n-aria={aria} aria-label={lang.attr(aria)}>
      <header>
        <h2 data-i18n={head}>{lang.text(head, literal)}</h2>
      </header>
      <div className="work">
        <div className="wrap" id={body} />
      </div>
    </section>
  )
}

/* The whole body, in the order page.html served it. Every parent transcribed
   here drops the whitespace the markup had between its children, because every
   one of them is a flex or a grid container or has none but block-level
   children (div.app, div.main, #split, .chat, .page, .page > header, .page
   .work and #capsPage's .wrap -- src/styles/page.css:227, 632, 2258, 2276,
   2896, 2911, 2929, 2930): a text node of pure whitespace is not a flex or grid
   item and does not paint between blocks, so dropping it moves no pixel.
   button#railShow's glyph is the same case (`.ghost-ic{display:grid}`,
   page.css:292), and whitespace inside an <svg> never paints at all. */
export function App(): JSX.Element {
  return createPortal(
    <>
      <div className="app">
        <Rail />
        <div className="main">
          <div className="split" id="split">
            {/* The session header belongs to the chat column, not the whole main
                pane: spanning both columns would leave a band of empty header
                above the workspace, which then reads as detached from the top. */}
            <div className="chat">
              <ChatTop />
              <Dock />
            </div>
            {/* the workspace: what the agent is touching right now */}
            <WsPane />
          </div>
        </div>
      </div>
      <RailShow />
      <CapsPage />
      <ModulePage id="xaPage" aria="gui.page.agents" head="gui.page.agents" literal="子智能体" body="xaBody" />
      {/* NOT a capability. A plugin is "what it can touch"; an entrance is
          "where you find it". Same brand can be both (Slack plugin vs Slack
          entrance) and the two point in opposite directions. */}
      <ModulePage id="connPage" aria="gui.page.conn" head="gui.page.conn" literal="入口" body="connBody" />
      <ModulePage id="memPage" aria="gui.mem.hero" head="gui.nav.mem" literal="记忆" body="memBody" />
      <ModulePage id="pbPage" aria="gui.nav.pb" head="gui.nav.pb" literal="剧本" body="pbBody" />
      <ModulePage id="kbPage" aria="gui.nav.kb" head="gui.nav.kb" literal="知识库" body="kbBody" />
      <ModulePage id="cronPage" aria="gui.page.cron" head="gui.page.cron" literal="定时" body="cronBody" />
      {/* The new-job sheet renders here from the cron island
          (src/features/cron/CronPage.tsx); only the veil is this file's. */}
      <div className="veil" id="jobVeil" data-open="false" />
      <DetailPanel />
      <SettingsModal />
      <ConfirmSheet />
      {/* One entry's credentials. Its own veil rather than the confirm dialog's:
          disconnecting from inside it raises that one, and a dialog cannot be
          both the thing asking and the thing asked. The sheet renders here from
          the connections island (src/features/connections/ConnPage.tsx). */}
      <div className="veil" id="connVeil" data-open="false" />
      <div className="menu" id="menu" data-open="false" role="menu" />
      <div className="toasts" id="toasts" aria-live="polite" />
      <ContextMenu />
      <Toasts />
      <SheetRack />
      <Tooltip />
      <Lightbox />
      <UpgradeShade />
      <FailureBars />
    </>,
    document.body
  )
}
