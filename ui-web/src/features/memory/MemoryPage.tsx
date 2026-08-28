import { useEffect, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { t } from '../../shell/bridge'
import * as store from './store'

import type { MemItem, MemKind, MemStats } from './types'
import type { CSSProperties, JSX } from 'react'

/* Four EverOS memory kinds behind one page: a stat band that doubles as
   the kind switch, a semantic search box, and a detail drawer carrying
   the one mutation memory supports today (delete, two-click armed). */
const MEM_KINDS: Array<{ kind: MemKind; tab: string; hint: string; stat: keyof MemStats }> = [
  { kind: 'episode', tab: 'gui.mem.tab_episode', hint: 'gui.mem.hint_episode', stat: 'episodes' },
  { kind: 'profile', tab: 'gui.mem.tab_profile', hint: 'gui.mem.hint_profile', stat: 'profiles' },
  { kind: 'agent_case', tab: 'gui.mem.tab_case', hint: 'gui.mem.hint_case', stat: 'agent_cases' },
  { kind: 'agent_skill', tab: 'gui.mem.tab_skill', hint: 'gui.mem.hint_skill', stat: 'agent_skills' },
]

function memWhen(iso?: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const two = (n: number) => String(n).padStart(2, '0')
  /* applyI18n mirrors LANG onto the document, which is as much of the
     legacy scope as an island can see. */
  return document.documentElement.lang.startsWith('zh')
    ? `${d.getMonth() + 1}月${d.getDate()}日 ${two(d.getHours())}:${two(d.getMinutes())}`
    : `${d.toLocaleString('en-US', { month: 'short' })} ${d.getDate()} ${two(d.getHours())}:${two(d.getMinutes())}`
}

const memPct = (v: number | null | undefined): string =>
  v == null ? '' : v <= 1 ? `${Math.round(v * 100)}%` : String(v)

function Meter({ v }: { v: number | null | undefined }): JSX.Element {
  const width = `${Math.round(Math.min(1, Math.max(0, Number(v) || 0)) * 100)}%`
  return (
    <span className="mmeter">
      <i style={{ width }} />
    </span>
  )
}

/* Same drawer tile as the plugin / skill pages, same formula: a stable
   per-name hue, so the same subject gets the same colour every render. */
function Tile({ name }: { name: string }): JSX.Element {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return <span className={'pmtile th' + (h % 8)}>{(name[0] || '?').toUpperCase()}</span>
}

/* Two-click armed delete, like the legacy memArm: the first click turns the
   button into its own confirm for four seconds, the second fires. */
function ArmedDelete({ onFire, style }: { onFire: () => void; style?: CSSProperties }): JSX.Element {
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return
    const id = setTimeout(() => setArmed(false), 4000)
    return () => clearTimeout(id)
  }, [armed])
  return (
    <button
      className={'mini ghost' + (armed ? ' danger' : '')}
      style={style}
      onClick={(e) => {
        e.stopPropagation()
        if (!armed) {
          setArmed(true)
          return
        }
        onFire()
      }}
    >
      {t(armed ? 'gui.mem.confirm_del' : 'gui.mem.delete')}
    </button>
  )
}

export function MemoryApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  /* Legacy chrome owns the drawer's closers (Esc, #dClose, click-outside)
     and they only flip #detail's data-open, so the island follows the flag
     to unmount its portal before another page's opener wipes #dBody. */
  useEffect(() => {
    const el = document.getElementById('detail')
    if (!el) return
    const ob = new MutationObserver(() => {
      if (el.dataset.open !== 'true') store.detailDismissed()
    })
    ob.observe(el, { attributes: true, attributeFilter: ['data-open'] })
    return () => ob.disconnect()
  }, [])
  return (
    <>
      <div className="pmhero">
        <h3>{t('gui.mem.hero')}</h3>
      </div>
      {s.phase === 'down' ? <div className="empty-note">{t('gui.mem.down')}</div> : <MemBody s={s} />}
      {s.detail ? <MemDetail it={s.detail} /> : null}
    </>
  )
}

function MemBody({ s }: { s: store.MemoryState }): JSX.Element {
  return (
    <>
      <div className="memstats">
        {MEM_KINDS.map((k) => (
          <button
            key={k.kind}
            className="mstat"
            aria-pressed={s.kind === k.kind}
            onClick={() => store.setKind(k.kind)}
          >
            <div className="k">{t(k.tab)}</div>
            <div className="v">{s.stats ? String(s.stats[k.stat]) : '—'}</div>
            <div className="h">{t(k.hint)}</div>
          </button>
        ))}
      </div>
      {s.kind !== 'profile' && (
        /* Keyed by kind: switching kind resets the query, and the remount
           is what clears the uncontrolled input. */
        <div className="memtools" key={s.kind}>
          <div className="cfind">
            <svg
              className="ic"
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              aria-hidden="true"
            >
              <circle cx="11" cy="11" r="7" />
              <path d="M20 20l-4.3-4.3" />
            </svg>
            <input
              placeholder={t('gui.mem.search_ph')}
              defaultValue={s.q}
              onInput={(e) => store.search(e.currentTarget.value.trim())}
            />
          </div>
          {s.phase === 'ready' && (
            <span className="n">{t(s.q ? 'gui.mem.n_hits' : 'gui.mem.n_total', { n: s.total })}</span>
          )}
        </div>
      )}
      {s.phase === 'error' ? (
        <>
          <div className="errline-lite">{`${t('gui.mem.down')} · ${s.err}`}</div>
          <button className="mini ghost" onClick={() => void store.load()}>
            {t('gui.plug.retry')}
          </button>
        </>
      ) : s.phase !== 'ready' && s.items.length === 0 ? (
        <div className="empty-note">{t('gui.hub.reading')}</div>
      ) : s.items.length === 0 ? (
        <div className="empty-note">{s.q ? t('gui.mem.none_found', { q: s.q }) : t('gui.mem.empty')}</div>
      ) : s.kind === 'profile' ? (
        <ProfileCard it={s.items[0]!} />
      ) : (
        <>
          <div className="memlist">
            {s.items.map((it) => (
              <MemRow key={it.id} it={it} />
            ))}
          </div>
          <Pager s={s} />
        </>
      )}
    </>
  )
}

function MemRow({ it }: { it: MemItem }): JSX.Element {
  const sub = it.kind === 'agent_case' ? it.key_insight || it.body : it.summary || it.body
  const hasMeta =
    typeof it.score === 'number' || it.kind === 'agent_skill' || (it.kind === 'agent_case' && it.quality_score != null)
  return (
    <div
      className="memrow"
      tabIndex={0}
      role="button"
      onClick={() => store.openDetail(it)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') store.openDetail(it)
      }}
    >
      <div className="t">
        <b>{it.subject || it.summary || it.id}</b>
        {it.timestamp ? <span className="when">{memWhen(it.timestamp)}</span> : null}
      </div>
      {sub ? <div className="s">{sub}</div> : null}
      {hasMeta && (
        <div className="meta">
          {typeof it.score === 'number' && <span className="pmsign faint">{it.score.toFixed(2)}</span>}
          {it.kind === 'agent_skill' && (
            <>
              <span className="pmcnt">{t('gui.mem.meta_confidence')}</span>
              <Meter v={it.confidence} />
              <span className="pmcnt">{t('gui.mem.meta_maturity')}</span>
              <Meter v={it.maturity_score} />
            </>
          )}
          {it.kind === 'agent_case' && it.quality_score != null && (
            <span className="pmsign faint">{`${t('gui.mem.meta_quality')} ${memPct(it.quality_score)}`}</span>
          )}
        </div>
      )}
    </div>
  )
}

/* profile_data values are engine-shaped: strings, arrays of objects,
   nested dicts, epoch stamps. Render all of them as prose lines. */
function memVal(v: unknown): string {
  if (v == null) return ''
  if (Array.isArray(v)) return v.map(memVal).filter(Boolean).join('\n')
  if (typeof v === 'object') {
    return Object.entries(v as Record<string, unknown>)
      .map(([k, x]) => `${k}: ${memVal(x)}`)
      .join(' · ')
  }
  return String(v)
}

function ProfileCard({ it }: { it: MemItem }): JSX.Element {
  const data = it.profile_data || {}
  return (
    <div>
      <div className="memkv">
        {Object.keys(data).map((k) => {
          const isStamp = /_ms$/i.test(k) && Number(data[k]) > 1e12
          return (
            <div className="row" key={k}>
              <div className="k">{k}</div>
              <div className="v">{isStamp ? memWhen(new Date(Number(data[k])).toISOString()) : memVal(data[k])}</div>
            </div>
          )
        })}
      </div>
      <ArmedDelete style={{ marginTop: '14px' }} onFire={() => store.remove(it)} />
      <div className="memnote">{t('gui.mem.del_profile_note')}</div>
    </div>
  )
}

function Pager({ s }: { s: store.MemoryState }): JSX.Element | null {
  const pages = Math.max(1, Math.ceil(s.total / store.MEM_PAGE_SIZE))
  if (s.q || pages <= 1) return null
  return (
    <div className="hubpage">
      <button className="mini ghost" disabled={s.page <= 1} onClick={() => store.pageBy(-1)}>
        {t('gui.mem.prev')}
      </button>
      <span className="pnote">{t('gui.mem.page', { p: s.page, n: pages })}</span>
      <button className="mini ghost" disabled={s.page >= pages} onClick={() => store.pageBy(1)}>
        {t('gui.mem.next')}
      </button>
    </div>
  )
}

function Section({ label, text }: { label: string; text: string }): JSX.Element {
  return (
    <div className="pmsec">
      <div className="cap">{label}</div>
      <div className="mempre">{text}</div>
    </div>
  )
}

/* The detail drawer's content, rendered into the shared #detail dialog the
   plugin and skill pages also use; the dialog chrome itself (title bar,
   close button, click-outside) stays legacy. */
function MemDetail({ it }: { it: MemItem }): JSX.Element | null {
  const host = store.detailHost()
  useEffect(() => {
    const title = document.getElementById('dTitle')
    if (title) title.textContent = ''
    const drawer = document.getElementById('detail')
    if (drawer) drawer.dataset.open = 'true'
  }, [it])
  const kindDef = MEM_KINDS.find((k) => k.kind === it.kind) ?? MEM_KINDS[0]!
  const name = it.subject || t(kindDef.tab)
  const metaRows: Array<[string, string]> = []
  if (it.session_id) metaRows.push([t('gui.mem.meta_session'), it.session_id])
  if (it.quality_score != null) metaRows.push([t('gui.mem.meta_quality'), memPct(it.quality_score)])
  if (it.confidence != null) metaRows.push([t('gui.mem.meta_confidence'), memPct(it.confidence)])
  if (it.maturity_score != null) metaRows.push([t('gui.mem.meta_maturity'), memPct(it.maturity_score)])
  return createPortal(
    <>
      <div className="pmdhead">
        <Tile name={name} />
        <div className="pmdmeta">
          <div className="l1">
            <b>{name}</b>
          </div>
          <div className="l2">{[t(kindDef.tab), memWhen(it.timestamp)].filter(Boolean).join(' · ')}</div>
        </div>
      </div>
      {metaRows.length > 0 && (
        <div>
          {metaRows.map(([k, v]) => (
            <div className="pnote" key={k}>{`${k} · ${v}`}</div>
          ))}
        </div>
      )}
      {it.kind === 'agent_case' ? (
        <>
          {it.body ? <Section label={t('gui.mem.sec_approach')} text={it.body} /> : null}
          {it.key_insight ? <Section label={t('gui.mem.sec_insight')} text={it.key_insight} /> : null}
        </>
      ) : (
        <Section label={t('gui.mem.sec_detail')} text={it.body || it.summary || ''} />
      )}
      <div className="pmsec">
        <ArmedDelete onFire={() => store.remove(it)} />
        {it.kind === 'episode' && <div className="memnote">{t('gui.mem.del_episode_note')}</div>}
      </div>
    </>,
    host,
  )
}
