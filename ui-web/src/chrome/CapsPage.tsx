/* The capabilities page's chrome: the title, the filter bar and the manual-add
 * fold that src/page.html used to carry as markup.
 *
 * One section serves two modules -- the skill market and the plugin market --
 * and what is here is everything around the body they share: the heading, the
 * search field, the status pills, the "installed" entry point each tab rides in
 * the bar, and the fold that registers a server by hand. The state is
 * src/state/caps.ts's, and a draw of either tab is what moves it.
 *
 * Every element below is a transcription -- tag, id, class, data-*, role, aria,
 * the svg path data and the text exactly as page.html spelled them, attributes
 * in the same order -- and src/test/__golden__/region-capsPage.txt is what says
 * so.
 *
 * THREE portals, not one. #capsBody sits between .cbar and details.adv inside
 * .wrap, and it is shared ground: the two islands attach their own hosts under
 * it and the tab draws clear it with innerHTML, so React must not own that
 * child list (see src/App.tsx for why a portal cannot leave a static sibling in
 * place). So .wrap, .work, the section, and the three containers below stay in
 * page.html until stage C14, and this renders their interiors.
 *
 * What this does NOT own, though it renders the elements:
 *   - #capsBody's children (the islands) and the three elements whose own
 *     attributes a draw writes: #capsPage[aria-label], #advAdd[hidden] and
 *     .cbar's display. Each is a container rather than a rendered node, so
 *     state/caps.ts writes it by hand and stays its one writer.
 *   - #pageHero, which is inserted before .cbar by the same store: it is a
 *     child of .wrap, so it cannot join a portal's list yet.
 * React diffs against the props it rendered last rather than against the
 * document, so none of those can be undone by a re-render here.
 *
 * #cq is deliberately uncontrolled, like the rail's search field: a component
 * owning it would re-render a text field the reader is typing into. Its two
 * listeners are native, on the element itself -- which is also what lets the
 * composition guard read the real KeyboardEvent -- and they dispatch on the
 * open tab, which is what the three layers the legacy chrome stacked on this
 * one field did (demo/150-chrome.js, then 152-skills.js, then 153-plugins.js).
 */

import { useEffect, useRef, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { composing } from '../features/composer/store'
import { islands } from '../islands'
import * as caps from '../state/caps'
import * as lang from '../state/lang'

import type { JSX } from 'react'

/* The four status pills, in page.html's order. Their `data-k` is what the
   filter reads and what the pressed flag compares against. */
const PILLS = [
  { k: 'all', key: 'gui.filter.all', literal: '全部' },
  { k: 'on', key: 'gui.filter.on', literal: '已启用' },
  { k: 'attn', key: 'gui.filter.todo', literal: '需要处理' },
  { k: 'add', key: 'gui.filter.add', literal: '可添加' },
] as const

/* The heading. A draw names it -- the installed view of either tab renames it
   -- and until one has, the literal the page was served with stands. The
   language pass writes this element too (it carries the key), so the value here
   comes from the same catalogue. */
function Title(): JSX.Element {
  const s = useSyncExternalStore(caps.subscribe, caps.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  return <h2 id="capsTitle" data-i18n="gui.tab.skills">{s.title ?? lang.text('gui.tab.skills', '技能')}</h2>
}

/* The search field. The placeholder is the only word on this page with no key
   of its own: every draw writes it, so the served literal is what stands until
   the first one. */
function Search(): JSX.Element {
  const s = useSyncExternalStore(caps.subscribe, caps.get)
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => {
    const el = field.current
    if (!el) return
    /* The old chain, as one handler: the plugin tab's query goes to the plugin
       island, the skill tab's to the skill island, and the bar's own term is
       the layer below both -- which no reachable state has, since the tab is
       only ever set to one of the two. */
    const typed = (): void => {
      const value = el.value.trim()
      const tab = caps.get().tab
      if (tab === 'plugin') {
        islands.plugins.setQuery(value)
        return
      }
      if (tab === 'skill') {
        islands.skills.setQuery(value)
        return
      }
      caps.setQuery(el.value.trim().toLowerCase())
    }
    /* Enter searches the hub now rather than after the debounce. An open
       composition sends Enter too -- it commits the candidate being typed --
       so that keystroke belongs to the input method. */
    const key = (e: KeyboardEvent): void => {
      if (composing(e)) return
      if (e.key !== 'Enter' || caps.get().tab !== 'skill') return
      e.preventDefault()
      islands.skills.searchNow(el.value.trim())
    }
    el.addEventListener('input', typed)
    el.addEventListener('keydown', key)
    return () => {
      el.removeEventListener('input', typed)
      el.removeEventListener('keydown', key)
    }
  }, [])
  return (
    <div className="cfind">
      {' '}
      <svg className="ic" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <circle cx="11" cy="11" r="7" /><path d="M20 20l-4.3-4.3" />
      </svg>
      {' '}
      <input id="cq" placeholder={s.search ?? '搜索'} data-i18n-aria="gui.search" ref={field} />
      {' '}
    </div>
  )
}

/* One tab's "installed" entry point, which rides in the bar rather than in the
   page: the count is the part's (demo/152-skills.js, 153-plugins.js), and the
   click is the island's view switch. Rendered only once its part has created
   it, and with no label until the first sync, which is how it was built. */
function InstalledButton({ btn, toggle }: { btn: caps.Installed; toggle: () => void }): JSX.Element {
  return (
    <button className="pminstbtn" hidden={btn.hidden} onClick={toggle}>
      {btn.label === null ? null : <span>{btn.label}</span>}
      {btn.badge ? <span className="pmbdg">{btn.badge}</span> : null}
    </button>
  )
}

/* The filter bar: the field, the status pills, and the two installed buttons in
   the order the two parts created them (the skill layer installs first). They
   are React's children rather than two appends, so a tab flip cannot reorder
   them. */
function FilterBar(): JSX.Element {
  const s = useSyncExternalStore(caps.subscribe, caps.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <>
      <Search />
      <div className="pills" id="cKind" role="group" data-i18n-aria="gui.filter_status" hidden={s.pillsHidden}>
        {PILLS.map((pill) => (
          <button
            key={pill.k}
            className="pill"
            data-k={pill.k}
            aria-pressed={s.kind === pill.k ? 'true' : 'false'}
            data-i18n={pill.key}
            onClick={() => caps.pick(pill.k)}
          >
            {lang.text(pill.key, pill.literal)}
          </button>
        ))}
      </div>
      {s.skill ? <InstalledButton btn={s.skill} toggle={() => islands.skills.toggleView()} /> : null}
      {s.plugin ? <InstalledButton btn={s.plugin} toggle={() => islands.plugins.toggleView()} /> : null}
    </>
  )
}

/* The manual add: what to do when the reader already knows the address. The
   fold's own `hidden` is a draw's (state/caps.ts) -- only the plugin market
   offers this -- and the two fields are read and cleared by the action. */
function ManualAdd(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <>
      <summary data-i18n="gui.adv_add">{lang.text('gui.adv_add', '高级 · 手动添加')}</summary>
      <p style={{ color: 'var(--muted)', fontSize: '12.5px', margin: '10px 0 0' }} data-i18n="gui.adv_hint">
        {lang.text('gui.adv_hint', '知道要接什么就直接填地址或启动命令，Raven 会握手取回它提供的动作。')}
      </p>
      <div className="row">
        <input id="mName" data-i18n-ph="gui.adv_name_ph" placeholder={lang.text('gui.adv_name_ph', '名称，例如 内部 CRM')} />
        <input id="mAddr" data-i18n-ph="gui.adv_addr_ph" placeholder={lang.text('gui.adv_addr_ph', '地址或命令，例如 npx -y @acme/crm-mcp')} />
        <button className="mini" id="mAdd" data-i18n="gui.add" onClick={() => void caps.manualAdd()}>{lang.text('gui.add', '添加')}</button>
      </div>
    </>
  )
}

/* The three interiors, each into the container page.html still provides.
   Guarded the way the island mounts are: a document without them renders
   nothing rather than throwing. */
export function CapsPage(): JSX.Element | null {
  const page = document.getElementById('capsPage')
  if (!page) return null
  const header = page.querySelector('header')
  const bar = page.querySelector('.cbar')
  const adv = document.getElementById('advAdd')
  return (
    <>
      {header ? createPortal(<Title />, header) : null}
      {bar ? createPortal(<FilterBar />, bar) : null}
      {adv ? createPortal(<ManualAdd />, adv) : null}
    </>
  )
}
