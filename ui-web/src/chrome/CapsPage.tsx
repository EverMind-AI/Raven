/* The capabilities page: the section src/page.html used to carry as markup --
 * the heading, the filter bar, the shared body and the manual-add fold.
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
 * The whole region, containers and all -- .wrap is a real child list now, which
 * is what lets the hero be a rendered first child rather than a node inserted
 * before the bar, and the section's aria-label, the fold's `hidden` and the
 * bar's `display` be rendered values rather than three writes by id.
 *
 * What this does NOT own, though it renders the element: #capsBody's children.
 * It is shared ground -- the two islands attach their own hosts under it and
 * each tab's draw clears it with innerHTML -- so React hands that box over
 * empty and never owns its child list. `data-open` is the same kind of thing
 * one level up: state/page.ts writes it on all seven sections, and what is
 * rendered here is the value the page is served with, which React then never
 * writes again because it diffs against its own last props.
 *
 * #cq is deliberately uncontrolled, like the rail's search field: a component
 * owning it would re-render a text field the reader is typing into. Its two
 * listeners are native, on the element itself -- which is also what lets the
 * composition guard read the real KeyboardEvent -- and they dispatch on the
 * open tab, which is the one thing the two tab renderers cannot decide for
 * themselves (features/skills/wire.ts, features/plugins/wire.ts).
 */

import { useEffect, useRef, useSyncExternalStore } from 'react'

import { composing } from '../features/composer/store'
import * as plugins from '../features/plugins/store'
import * as skills from '../features/skills/store'
import { t } from '../i18n/t'
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
  return <h2 id="capsTitle">{s.title ?? t('gui.tab.skills')}</h2>
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
        plugins.setQuery(value)
        return
      }
      if (tab === 'skill') {
        skills.setQuery(value)
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
      skills.searchNow(el.value.trim())
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
      <input id="cq" placeholder={s.search ?? '搜索'} aria-label={lang.attr('gui.search')} ref={field} />
      {' '}
    </div>
  )
}

/* One tab's "installed" entry point, which rides in the bar rather than in the
   page: the count is the tab renderer's (features/skills/wire.ts,
   features/plugins/wire.ts) and the click is the island's view switch. Rendered
   only once its renderer has asked for it, and with no label until the first
   sync, which is when the count is known. */
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
      <div className="pills" id="cKind" role="group" aria-label={lang.attr('gui.filter_status')} hidden={s.pillsHidden}>
        {PILLS.map((pill) => (
          <button
            key={pill.k}
            className="pill"
            data-k={pill.k}
            aria-pressed={s.kind === pill.k ? 'true' : 'false'}
            onClick={() => caps.pick(pill.k)}
          >
            {t(pill.key)}
          </button>
        ))}
      </div>
      {s.skill ? <InstalledButton btn={s.skill} toggle={() => skills.toggleView()} /> : null}
      {s.plugin ? <InstalledButton btn={s.plugin} toggle={() => plugins.toggleView()} /> : null}
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
      <summary>{t('gui.adv_add')}</summary>
      <p style={{ color: 'var(--muted)', fontSize: '12.5px', margin: '10px 0 0' }}>
        {t('gui.adv_hint')}
      </p>
      <div className="row">
        <input id="mName" placeholder={t('gui.adv_name_ph')} />
        <input id="mAddr" placeholder={t('gui.adv_addr_ph')} />
        <button className="mini" id="mAdd" onClick={() => void caps.manualAdd()}>{t('gui.add')}</button>
      </div>
    </>
  )
}

/* The section, in the order page.html had it. Two modules, one heading, one
   filter bar and one shared body between them.

   The heading is drawn and then hidden (`.page > header h2{display:none}`,
   src/styles/page.css:2922): the strip stays for breathing room and the scroll
   fade, and the hero right below says the name bigger. */
export function CapsPage(): JSX.Element {
  const s = useSyncExternalStore(caps.subscribe, caps.get)
  return (
    <section className="page" id="capsPage" data-open="false" aria-label={s.label ?? undefined}>
      <header>
        <Title />
      </header>
      <div className="work">
        <div className="wrap">
          {s.hero === null ? null : (
            <div className="pmhero" id="pageHero" hidden={!s.hero}>
              {s.hero ? <h3>{s.hero}</h3> : null}
            </div>
          )}
          <div className="cbar" style={s.bar === null ? undefined : { display: s.bar }}>
            <FilterBar />
          </div>
          <div id="capsBody" />
          <details className="adv" id="advAdd" hidden={s.advHidden}>
            <ManualAdd />
          </details>
        </div>
      </div>
    </section>
  )
}
