import { useSyncExternalStore } from 'react'

import {
  TwoPane, TwoPaneFind, TwoPaneHead, TwoPaneList, TwoPaneNone, TwoPaneRow,
} from '../../components/TwoPane'
import { t } from '../../i18n/t'
import { subscribe as langSubscribe, tag as langTag } from '../../state/lang'
import * as store from './store'

import type { MemItem, MemKind, MemStats } from './types'
import type { JSX } from 'react'

/* Four EverOS memory kinds behind one section of the settings dialog: a kind
   switch that carries the counts, a semantic search box, the rows, and beside
   them the picked memory, read-only. */
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
  /* The page's language declaration, which is the only part of the language the
     date line cares about. Read here rather than passed in because this is not
     a component; MemoryApp subscribes to the store for the repaint. */
  return langTag().startsWith('zh')
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

export function MemoryApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* The language the page resolved, so a pick repaints this island: memWhen
     above and every word below read it at render time (state/lang/store.ts).
     This subscription is what carries the repaint -- the whole-page redraw no
     longer lists this island. */
  useSyncExternalStore(langSubscribe, langTag)
  if (s.phase === 'down') return <div className="empty-note">{t('gui.mem.down')}</div>
  /* Not a failure and not an empty store: this install keeps its memories
     somewhere this page does not read. Saying so beats four zeros, which a
     reader takes for loss. */
  if (s.note) return <div className="empty-note">{s.note}</div>
  return (
    <TwoPane side={<MemSide s={s} />}>
      {s.kind === 'profile' ? (
        s.items[0] ? <ProfileCard it={s.items[0]} /> : <TwoPaneNone>{t('gui.mem.empty')}</TwoPaneNone>
      ) : s.detail ? (
        <MemDetail it={s.detail} />
      ) : (
        /* "Nothing here yet" is the list's line, not this one: said in both
           columns it reads as two separate emptinesses. */
        <TwoPaneNone>{t('gui.mem.pick')}</TwoPaneNone>
      )}
    </TwoPane>
  )
}

/* The left column: which kind, then the search over it, then the rows.
   The four kinds carry their counts, because "how much of this is there" is
   the question the switch is asked while it is being used as a switch. */
function MemSide({ s }: { s: store.MemoryState }): JSX.Element {
  const kindDef = MEM_KINDS.find((k) => k.kind === s.kind) ?? MEM_KINDS[0]!
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
          </button>
        ))}
      </div>
      <div className="memnote">{t(kindDef.hint)}</div>
      {s.kind === 'profile' ? null : (
        /* Keyed by kind: switching kind resets the query, and the remount is
           what clears the field. */
        <TwoPaneFind
          key={s.kind}
          value={s.q}
          onChange={(v) => store.search(v.trim())}
          placeholder={t('gui.mem.search_ph')}
        />
      )}
      <TwoPaneList>
        {s.kind === 'profile' ? null : s.phase === 'error' ? (
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
        ) : (
          s.items.map((it) => (
            <TwoPaneRow
              key={it.id}
              current={s.detail?.id === it.id}
              name={it.subject || it.summary || it.id}
              sub={(it.kind === 'agent_case' ? it.key_insight || it.body : it.summary || it.body) || ''}
              {...(it.timestamp ? { when: memWhen(it.timestamp) } : {})}
              onOpen={() => store.openDetail(it)}
            />
          ))
        )}
      </TwoPaneList>
      {s.kind === 'profile' ? null : <Pager s={s} />}
      {s.phase === 'ready' && s.kind !== 'profile' ? (
        <div className="memnote">{t(s.q ? 'gui.mem.n_hits' : 'gui.mem.n_total', { n: s.total })}</div>
      ) : null}
    </>
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

/* The picked memory, in the right column. It used to be the shared drawer the
   plugin and skill pages also open; inside the settings dialog a drawer is a
   layer over a layer, and the list it covered is what a reader comparing two
   memories needs to keep. */
function MemDetail({ it }: { it: MemItem }): JSX.Element {
  const kindDef = MEM_KINDS.find((k) => k.kind === it.kind) ?? MEM_KINDS[0]!
  const name = it.subject || t(kindDef.tab)
  const metaRows: Array<[string, string]> = []
  if (it.session_id) metaRows.push([t('gui.mem.meta_session'), it.session_id])
  if (it.quality_score != null) metaRows.push([t('gui.mem.meta_quality'), memPct(it.quality_score)])
  /* Confidence and maturity are the two a reader compares between skills, so
     they are bars rather than two more numbers in the meta line. */
  const bars: Array<[string, number | null | undefined]> = []
  if (it.confidence != null) bars.push([t('gui.mem.meta_confidence'), it.confidence])
  if (it.maturity_score != null) bars.push([t('gui.mem.meta_maturity'), it.maturity_score])
  return (
    <>
      <TwoPaneHead
        icon={<Tile name={name} />}
        name={name}
        meta={
          <>
            <span>{[t(kindDef.tab), memWhen(it.timestamp)].filter(Boolean).join(' · ')}</span>
            {metaRows.map(([k, v]) => (
              <span className="pmcnt" key={k}>{`${k} · ${v}`}</span>
            ))}
          </>
        }
      />
      {bars.length ? (
        <div className="meta">
          {bars.map(([k, v]) => (
            <span key={k}>
              <span className="pmcnt">{k}</span>
              <Meter v={v} />
            </span>
          ))}
        </div>
      ) : null}
      {it.kind === 'agent_case' ? (
        <>
          {it.body ? <Section label={t('gui.mem.sec_approach')} text={it.body} /> : null}
          {it.key_insight ? <Section label={t('gui.mem.sec_insight')} text={it.key_insight} /> : null}
        </>
      ) : (
        <Section label={t('gui.mem.sec_detail')} text={it.body || it.summary || ''} />
      )}
    </>
  )
}
