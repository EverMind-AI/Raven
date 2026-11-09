import { useState, useSyncExternalStore } from 'react'

import {
  TwoPane, TwoPaneFind, TwoPaneHead, TwoPaneList, TwoPaneNoHit, TwoPaneNone, TwoPaneRow, TwoPaneWait,
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

const MEM_GLYPH = (
  <svg viewBox="0 0 24 24"><path d="m12 4.5 8 4-8 4-8-4z" /><path d="m4 12.5 8 4 8-4" /><path d="m4 16.5 8 4 8-4" /></svg>
)

/* Which EverOS this section reads. Two segments rather than a checkbox: the
   question has two answers, and "off" is the wizard's choice, not this section's.
   A backend whose plugin is not installed keeps its segment, disabled -- the
   reason it cannot be picked is the useful part. */
const BACKENDS: Array<{ name: string; label: string }> = [
  { name: 'everos', label: 'gui.mem.src_local' },
  { name: 'everos-cloud', label: 'gui.mem.src_cloud' },
]

/* Whether the install told us enough to draw a switch. A fixture or a failed
   stats read answers nothing, and two segments over no data would offer a
   write the section cannot justify. */
const canSwitch = (s: store.MemoryState): boolean =>
  Boolean(s.stats?.backend) || Boolean(s.stats?.available_backends?.length)

function BackendSwitch({ s }: { s: store.MemoryState }): JSX.Element | null {
  if (!canSwitch(s)) return null
  const available = new Set(s.stats?.available_backends ?? [])
  return (
    <>
      <div className="memsrc" role="group" aria-label={t('gui.mem.src')}>
        {BACKENDS.map((b) => (
          <button
            key={b.name}
            type="button"
            className="memsrcbtn"
            aria-pressed={s.stats?.backend === b.name}
            disabled={s.backendBusy || !available.has(b.name)}
            title={available.has(b.name) ? undefined : t('gui.mem.src_missing')}
            onClick={() => store.switchBackend(b.name)}
          >
            {t(b.label)}
          </button>
        ))}
      </div>
      {s.backendNotice ? <div className="memnote">{s.backendNotice}</div> : null}
      {s.backendErr ? <div className="memnote memerr">{s.backendErr}</div> : null}
    </>
  )
}

/* A working key can still be replaced: the link opens the same panel over a
   store that already answers. Without it the only way to change the key would
   be the settings card this section no longer draws. */
function ChangeCloudKey({ s }: { s: store.MemoryState }): JSX.Element | null {
  if (s.switching || s.stats?.backend !== 'everos-cloud' || s.stats?.cloud_key_set !== true || s.cloudRejected) return null
  return (
    <button type="button" className="memchange" onClick={() => store.startCloudEdit()}>
      {t('gui.mem.cloud_change')}
    </button>
  )
}

/* The cloud backend with no key: the section has nothing to read, and the store's
   error frame would blame the service for a step the reader has not taken.
   One field, one button, one way to get a key -- the panel is the whole column
   until the key is on file. */
function CloudConnect({ s }: { s: store.MemoryState }): JSX.Element {
  const [key, setKey] = useState('')
  const save = () => store.saveCloudKey(key)
  /* The same panel serves "there is no key" and "replace the one on file";
     the copy is the only thing that changes. */
  const editing = s.stats?.cloud_key_set === true
  return (
    <div className="memconnect">
      <div className="memconnect-inner">
        <div className="memconnect-glyph" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M7.5 18.5h9a4 4 0 0 0 .6-7.95A6 6 0 0 0 5.6 9.4a4.55 4.55 0 0 0 1.9 9.1Z" />
          </svg>
        </div>
        <div className="memconnect-t">{t(editing ? 'gui.mem.cloud_change_t' : 'gui.mem.cloud_t')}</div>
        <div className="memconnect-sub">{t(editing ? 'gui.mem.cloud_change_sub' : 'gui.mem.cloud_sub')}</div>
        <div className="memconnect-row">
          <input
            className="memconnect-in"
            type="password"
            value={key}
            placeholder={t('gui.mem.cloud_ph')}
            aria-label={t('gui.mem.cloud_ph')}
            autoComplete="off"
            spellCheck={false}
            disabled={s.cloudBusy}
            onChange={(e) => setKey(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') save()
            }}
          />
          <button className="mini" disabled={s.cloudBusy || !key.trim()} onClick={save}>
            {s.cloudBusy ? (
              <>
                <span className="memconnect-spin" aria-hidden="true" />
                {t('gui.mem.cloud_checking')}
              </>
            ) : (
              t('gui.mem.cloud_save')
            )}
          </button>
        </div>
        {s.cloudErr ? <div className="memnote memerr">{s.cloudErr}</div> : null}
        <div className="memnote memconnect-link">
          <a href="https://everos.evermind.ai" target="_blank" rel="noreferrer">
            {t('gui.mem.cloud_get')}
          </a>
        </div>
      </div>
    </div>
  )
}

/* The local backend with no server: the same shape as the cloud key panel,
   because it is the same situation -- no store to read, and one action that
   fixes it. The backend starts this server at boot; this is for after that
   process went away. */
function LocalStart({ s }: { s: store.MemoryState }): JSX.Element {
  /* The plugin does not start a root the user manages, so a Start here could
     only be refused. The step is named as theirs, and the button looks again
     once they have taken it. Whose root it is stays the plugin's answer. */
  const theirs = s.stats?.local_startable === false
  return (
    <div className="memconnect">
      <div className="memconnect-inner">
        <div className="memconnect-glyph" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M4.5 6.5h15v11h-15z" />
            <path d="M4.5 10.5h15" />
            <path d="M7 14h3" />
          </svg>
        </div>
        <div className="memconnect-t">{t('gui.mem.local_t')}</div>
        <div className="memconnect-sub">{t(theirs ? 'gui.mem.local_foreign_sub' : 'gui.mem.local_sub')}</div>
        <div className="memconnect-row memconnect-actions">
          {theirs ? (
            <button className="mini" onClick={() => store.recheckLocal()}>
              {t('gui.mem.local_recheck')}
            </button>
          ) : (
            <button className="mini" disabled={s.localBusy} onClick={() => store.startLocal()}>
              {s.localBusy ? (
                <>
                  <span className="memconnect-spin" aria-hidden="true" />
                  {t('gui.mem.local_starting')}
                </>
              ) : (
                t('gui.mem.local_start')
              )}
            </button>
          )}
        </div>
        {s.localErr ? <div className="memnote memerr">{s.localErr}</div> : null}
      </div>
    </div>
  )
}

export function MemoryApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* The language the page resolved, so a pick repaints this island: memWhen
     above and every word below read it at render time (state/lang/store.ts).
     This subscription is what carries the repaint -- the whole-page redraw no
     longer lists this island. */
  useSyncExternalStore(langSubscribe, langTag)
  /* The names this install can read through. A selected backend missing here
     has no plugin behind it, and its note is the one thing to say: the key and
     Start panels would offer steps for a client that cannot exist. */
  const available = s.stats?.available_backends ?? []
  if (s.phase === 'down') {
    return (
      <TwoPane>
        <TwoPaneNone icon={MEM_GLYPH} title={t('gui.mem.off_t')}>{t('gui.mem.down')}</TwoPaneNone>
      </TwoPane>
    )
  }
  /* The cloud backend with no key is not a service failure: every read would
     answer 401, and the error frame would ask the reader to retry something
     only a key can fix. The panel takes the whole column instead -- but only
     when the cloud backend exists to take a key. */
  if (
    !s.switching
    && s.stats?.backend === 'everos-cloud'
    && available.includes('everos-cloud')
    && (s.stats?.cloud_key_set === false || s.cloudRejected || s.cloudEditing)
  ) {
    return (
      <TwoPane side={canSwitch(s) ? <BackendSwitch s={s} /> : undefined}>
        <CloudConnect s={s} />
      </TwoPane>
    )
  }
  /* The local backend with no server: the store would answer only zeros, so
     the column is the one action that brings it back -- when the plugin that
     runs the server is actually installed. */
  if (
    !s.switching
    && s.stats?.backend === 'everos'
    && available.includes('everos')
    && s.stats?.local_running === false
  ) {
    return (
      <TwoPane side={canSwitch(s) ? <BackendSwitch s={s} /> : undefined}>
        <LocalStart s={s} />
      </TwoPane>
    )
  }
  /* Not a failure and not an empty store: this install keeps its memories
     somewhere this page does not read. Saying so beats four zeros, which a
     reader takes for loss. The switch stays beside it: leaving a backend that
     is missing or turned off is exactly what a reader needs here. */
  if (!s.switching && s.note) {
    return (
      <TwoPane side={canSwitch(s) ? <BackendSwitch s={s} /> : undefined}>
        <TwoPaneNone icon={MEM_GLYPH} title={t('gui.mem.off_t')}>{s.note}</TwoPaneNone>
      </TwoPane>
    )
  }
  /* A read that failed is the service being down, not an empty store -- an
     empty store answers, with no rows -- and it leaves the kind switch, the
     search and the list nothing to work on, their counts dashes. So it takes
     the whole frame, with the retry as its one control, instead of a red
     paragraph and a full-width button squeezed under a search box. */
  if (!s.switching && s.phase === 'error') {
    return (
      <TwoPane side={canSwitch(s) ? <BackendSwitch s={s} /> : undefined}>
        <TwoPaneNone
          icon={MEM_GLYPH}
          title={t('gui.mem.err_t')}
          detail={s.err}
          action={{ label: t('gui.plug.retry'), onClick: () => void Promise.all([store.load(), store.refreshStats()]) }}
        >
          {t('gui.mem.err_sub')}
        </TwoPaneNone>
      </TwoPane>
    )
  }
  const none = <TwoPaneNone icon={MEM_GLYPH} title={t('gui.mem.empty_t')}>{t('gui.mem.empty')}</TwoPaneNone>
  return (
    <TwoPane side={<MemSide s={s} />}>
      {s.switching ? (
        /* The switch's wait lives in the pane whose contents are changing; the
           left column keeps the shape it had, so the switch and the counts do
           not jump under the pointer. */
        <div className="memconnect">
          <div className="memconnect-inner">
            <div className="memconnect-glyph" aria-hidden="true">
              <span className="memconnect-spin" />
            </div>
            <div className="memconnect-t">{t('gui.mem.switching')}</div>
          </div>
        </div>
      ) : s.kind === 'profile' ? (
        s.items[0] ? <ProfileCard it={s.items[0]} /> : s.phase === 'ready' ? none : null
      ) : s.detail ? (
        <MemDetail it={s.detail} />
      ) : s.phase === 'ready' && !s.items.length && !s.q ? (
        /* "Nothing here yet" is this column's, and the list says nothing: said
           in both columns it reads as two separate emptinesses. */
        none
      ) : (s.phase === 'idle' || s.phase === 'loading') && !s.items.length ? (
        /* The first read is still out: "Pick a memory" here flashed for a
           moment before the key panel or the rows arrived, on every entry. */
        null
      ) : (
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
      <BackendSwitch s={s} />
      <ChangeCloudKey s={s} />
      <div className="memstats">
        {MEM_KINDS.map((k) => (
          <button
            key={k.kind}
            className="mstat"
            aria-pressed={s.kind === k.kind}
            onClick={() => store.setKind(k.kind)}
          >
            <div className="k">{t(k.tab)}</div>
            <div className="v">{s.switching ? '—' : s.stats ? String(s.stats[k.stat]) : '—'}</div>
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
      {/* Keyed by page: a new page starts at its first row, not wherever the
          last one was scrolled to. */}
      <TwoPaneList key={s.page}>
        {s.kind === 'profile' ? null : s.phase !== 'ready' && s.items.length === 0 ? (
          <TwoPaneWait />
        ) : s.items.length === 0 ? (
          s.q ? <TwoPaneNoHit>{t('gui.mem.none_found', { q: s.q })}</TwoPaneNoHit> : null
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
      {/* Kept through a page turn's reload: the rows stay up while the next
          page is read, and dropping this line for that moment moved the pager
          under the pointer that had just clicked it. Absent with no rows: "0
          total" under an empty list is a number about nothing, and the empty
          state beside it already says so. */}
      {s.kind !== 'profile' && s.items.length > 0 && (s.phase === 'ready' || s.phase === 'loading') ? (
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
