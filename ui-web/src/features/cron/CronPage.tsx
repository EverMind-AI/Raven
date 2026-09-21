import { useEffect, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { SheetFoot } from '../../components/SetupSheet'
import {
  TwoPane, TwoPaneFind, TwoPaneGroup, TwoPaneHead, TwoPaneList, TwoPaneNone, TwoPaneRow, TwoPaneSection,
  TwoPaneSwitch,
} from '../../components/TwoPane'
import { t } from '../../i18n/t'
import { ask as confirmAsk } from '../../state/confirm'
import * as lang from '../../state/lang'
import { show as menuAt } from '../../state/menu'
import * as settingsDialog from '../../state/settings'
import { show as toast } from '../../state/toast'
import { cronExprHuman, cronWhen } from './humanize'
import * as store from './store'

import type { CronDraft, CronJob, CronRun } from './types'
import type { JSX } from 'react'

/* The frequencies and the delivery routes the editor offers. Page data, not
   wire data: a job's own kind and expression come from `cron.list`, and these
   are the choices the form can express them as. */
const FREQ: Array<{ id: CronJob['freq']; label: string }> = [
  { id: 'hour', label: 'gui.freq.hour' },
  { id: 'day', label: 'gui.freq.day' },
  { id: 'week', label: 'gui.freq.week' },
  { id: 'once', label: 'gui.freq.once' },
  { id: 'cron', label: 'gui.freq.cron' },
]
const DELIVER: Record<string, string> = {
  app: 'gui.deliver.app',
  feishu: 'gui.deliver.feishu',
  email: 'gui.deliver.email',
}

/* Which message a refusal shows, keyed by what a source could not read. The
   redraw lands the note under the control the reader has to fix; a toast
   would not, because live mode sends those to the console. */
const JOB_BAD: Record<string, string> = {
  'no instant': 'gui.job.need_instant',
  'bad weekday': 'gui.job.bad_weekday',
}

function jobRefuse(draft: CronDraft, err: unknown): void {
  if (err && (err as { handled?: boolean }).handled) return
  draft.bad = JOB_BAD[(err as Error | null)?.message ?? ''] || 'gui.job.bad_time'
  store.redraw()
}

/* A delete that failed leaves the reader on the job they were looking at:
   the source has already said why, and what must not happen is the success
   branch running anyway and bouncing them out to a list where the row is
   still there. */
function keepPlace(err: unknown): void {
  if (err && (err as { handled?: boolean }).handled) return
  console.error('cron delete', err)
}

function removeThenList(j: CronJob): void {
  confirmAsk(
    t('gui.cron.delete_title'),
    t('gui.cron.delete_body', { name: j.name }),
    t('gui.cron.delete'),
    () =>
      store
        .source()
        .remove(j)
        .then(() => {
          store.backToList()
          void store.refresh()
        })
        .catch(keepPlace),
  )
}

export function CronApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* The language the page resolved, so a pick repaints this island: every word
     below is a t(key) read at render time (state/lang/store.ts). The detail
     also refetches its run history on it -- run stamps arrive language-baked
     from the source, so a flip has to ask again, while the plain redraws the
     island's own controls ask for must not. */
  const { lang: pageLang } = useSyncExternalStore(lang.subscribe, lang.get)
  const [q, setQ] = useState('')
  const job = s.viewId ? s.rows.find((x) => x.id === s.viewId) : undefined
  useEffect(() => {
    if (s.viewId && !job) store.backToList()
  }, [s.viewId, job])
  const draft = job && s.draft && s.draft.id === job.id ? s.draft : null
  return (
    <>
      <TwoPane side={<CronSide rows={s.rows} loaded={s.loaded} q={q} onQ={setQ} viewId={s.viewId} />}>
        {job && draft ? (
          <CronDetail key={`${job.id}:${s.epoch}`} job={job} draft={draft} rev={s.rev} lang={pageLang} />
        ) : (
          /* "Nothing here yet" is the list's line, not this one: said in both
             columns it reads as two separate emptinesses. */
          <TwoPaneNone>{t('gui.cron.pick')}</TwoPaneNone>
        )}
      </TwoPane>
      {s.sheet ? <JobSheet key={`sheet:${s.epoch}`} draft={s.sheet} /> : null}
    </>
  )
}

const failing = (j: CronJob): boolean => j.on && !!j.runs[0] && !j.runs[0].ok

/* The left column: the search, the button that adds one, and the jobs in two
   runs -- the ones that are on, then the ones that are not. Two groups rather
   than three filter chips: what a reader arrives asking is "did anything
   break", and a job whose last run failed says so on its own second line, in
   the colour, where a chip could only ever say how many. */
function CronSide({ rows, loaded, q, onQ, viewId }: {
  rows: CronJob[]
  loaded: boolean
  q: string
  onQ(v: string): void
  viewId: string | null
}): JSX.Element {
  const term = q.trim().toLowerCase()
  const hit = (j: CronJob): boolean =>
    !term || j.name.toLowerCase().includes(term) || j.what.toLowerCase().includes(term)
  const shown = rows.filter(hit)
  const on = shown.filter((j) => j.on)
  const off = shown.filter((j) => !j.on)
  const row = (j: CronJob): JSX.Element => {
    const last = j.runs[0]
    const broke = failing(j)
    return (
      <TwoPaneRow
        key={j.id}
        current={j.id === viewId}
        off={!j.on}
        name={j.name}
        sub={broke && last ? `${t('gui.cron.failed')} · ${last.at}` : cronWhen(j)}
        {...(broke ? { tone: 'bad' as const } : {})}
        onOpen={() => store.openDetail(j)}
        trailing={
          <TwoPaneSwitch
            on={j.on}
            label={t('gui.caps.toggle_aria', { name: j.name })}
            onChange={() => void store.source().toggle(j).then(() => store.refresh())}
          />
        }
      />
    )
  }
  return (
    <>
      <TwoPaneFind
        value={q}
        onChange={onQ}
        placeholder={t('gui.cron.search')}
        onAdd={() => store.openSheet()}
        addLabel={t('gui.cron_new')}
      />
      <TwoPaneList>
        {!loaded && !rows.length ? null : shown.length === 0 ? (
          <div className="empty-note">{t(rows.length ? 'gui.cron.f_none' : 'gui.cron.none')}</div>
        ) : (
          <>
            {on.length ? <TwoPaneGroup>{t('gui.cron.g_on')}</TwoPaneGroup> : null}
            {on.map(row)}
            {off.length ? <TwoPaneGroup>{t('gui.cron.g_off')}</TwoPaneGroup> : null}
            {off.map(row)}
          </>
        )}
      </TwoPaneList>
    </>
  )
}

/* The job itself, in the right column: what it is set to do, and what it has
   done. One scroll rather than two tabs -- beside its own list the form is
   half the width it used to be and the history fits under it, and the delete
   that used to sit between them is in the header's menu. */
function CronDetail({ job, draft, rev, lang }: { job: CronJob; draft: CronDraft; rev: number; lang: string }): JSX.Element {
  const [runs, setRuns] = useState<CronRun[] | null>(null)
  const [running, setRunning] = useState(false)
  /* Keyed on rev and on the page's language, not just on the job. `rev` is
     what the live cron.finished handler bumps through refresh(), precisely so
     a run that lands while the reader is on this page drops into the list; the
     language is what makes a flip re-ask for stamps the source baked words
     into. */
  useEffect(() => {
    let stale = false
    store
      .source()
      .runs(job)
      .then((rows) => {
        if (!stale) setRuns(rows)
      })
      .catch(() => {
        if (!stale) setRuns([])
      })
    return () => {
      stale = true
    }
  }, [job.id, rev, lang])
  const save = (): void => {
    if (!draft.name.trim() || !draft.what.trim()) {
      draft.blank = true
      store.redraw()
      return
    }
    store
      .source()
      .save(draft)
      .then((saved) => {
        store.viewSaved(saved)
        toast(t('gui.cron.saved'))
      })
      .catch((e: unknown) => jobRefuse(draft, e))
  }
  const runNow = (): void => {
    setRunning(true)
    void store
      .source()
      .runNow(job)
      .then(() => store.refresh())
      .finally(() => setRunning(false))
  }
  return (
    <>
      <TwoPaneHead
        name={job.name}
        meta={
          <>
            <span>{cronWhen(job)}</span>
            <span className={job.on ? 'st ok' : 'st'}>
              {job.on ? t('gui.cron.next_only', { next: job.next }) : t('gui.cron.paused')}
            </span>
          </>
        }
        aside={
          <>
            <TwoPaneSwitch
              on={job.on}
              label={t('gui.caps.toggle_aria', { name: job.name })}
              onChange={() => void store.source().toggle(job).then(() => store.refresh())}
            />
            <button
              className="mini ghost"
              aria-label={t('gui.cron.menu_aria', { name: job.name })}
              onClick={(e) => {
                const b = e.currentTarget.getBoundingClientRect()
                menuAt(b.right - 150, b.bottom + 6, [
                  { label: t('gui.cron.duplicate'), fn: () => store.openSheet(job) },
                  { label: t('gui.cron.delete'), bad: true, fn: () => removeThenList(job) },
                ])
              }}
            >
              &#8943;
            </button>
          </>
        }
      />
      <JobForm draft={draft} />
      <SheetFoot label={t('gui.cron.save')} onSave={save} />
      <TwoPaneSection
        label={t('gui.cron.tab_runs')}
        act={
          <button className="mini" disabled={running} onClick={runNow}>
            {t(running ? 'gui.cron.running_now' : 'gui.cron.run_now')}
          </button>
        }
      >
        <div className="cdruns">
          {runs === null ? null : runs.length === 0 ? (
            <div className="empty-note">{t('gui.cron.hist_none')}</div>
          ) : (
            runs.map((run, i) => (
              <button key={i} className="cdrun" onClick={() => void store.source().openRun(job, run)}>
                <span className={'st' + (run.ok ? ' ok' : ' bad')} />
                <span className="at">{run.at}</span>
                <span className="note">{run.note || ''}</span>
                <span className="chev">&rsaquo;</span>
              </button>
            ))
          )}
        </div>
      </TwoPaneSection>
    </>
  )
}

/* create / edit -- one form, two hosts: the new-job sheet and the detail
   page's config card both edit the same draft shape.

   Inputs are uncontrolled on purpose: a keystroke mutates the draft object and
   re-renders nothing, so focus and IME composition survive; only a frequency
   change or a refusal redraws. The
   store's epoch key remounts this subtree whenever a draft is replaced. */
function JobForm({ draft }: { draft: CronDraft }): JSX.Element {
  if (draft.freq === 'week' && draft.wd == null) draft.wd = 1
  const blankName = Boolean(draft.blank) && !draft.name.trim()
  const blankWhat = Boolean(draft.blank) && !draft.what.trim()
  const whatStyle = {
    background: 'var(--ink)',
    border: '1px solid var(--line)',
    borderRadius: 7,
    padding: '9px 11px',
    outline: 0,
    resize: 'vertical' as const,
    fontSize: 13,
    lineHeight: 1.6,
  }
  return (
    <>
      <div className="ff">
        <label>{t('gui.job.name')}</label>
        <input
          type="text"
          defaultValue={draft.name}
          placeholder={t('gui.job.name_ph')}
          data-bad={blankName ? 'true' : undefined}
          onInput={(e) => {
            draft.name = e.currentTarget.value
            draft.blank = null
          }}
        />
        {blankName && <span className="e">{t('gui.job.need_name')}</span>}
      </div>
      <div className="ff">
        <label>{t('gui.job.what')}</label>
        <textarea
          rows={3}
          defaultValue={draft.what}
          placeholder={t('gui.job.what_ph')}
          data-bad={blankWhat ? 'true' : undefined}
          style={blankWhat ? { ...whatStyle, borderColor: 'var(--amber)' } : whatStyle}
          onInput={(e) => {
            draft.what = e.currentTarget.value
            draft.blank = null
          }}
        />
        {blankWhat && <span className="e">{t('gui.job.need_what')}</span>}
      </div>
      <div className="ff">
        <label>{t('gui.job.freq')}</label>
        <div
          style={{ display: 'flex', gap: 9, alignItems: 'center', flexWrap: 'wrap' }}
          data-bad={draft.bad ? 'true' : undefined}
        >
          <div className="seg">
            {FREQ.map((f) => (
              <button
                key={f.id}
                aria-pressed={f.id === draft.freq}
                onClick={() => {
                  draft.freq = f.id
                  store.redraw()
                }}
              >
                {t(f.label)}
              </button>
            ))}
          </div>
          {draft.freq === 'week' && (
            <select
              defaultValue={String(draft.wd ?? 1)}
              onChange={(e) => {
                draft.wd = Number(e.currentTarget.value)
                draft.bad = null
              }}
            >
              {[0, 1, 2, 3, 4, 5, 6].map((d) => (
                <option key={d} value={String(d)}>
                  {t('gui.cron.h.dow' + d)}
                </option>
              ))}
            </select>
          )}
          {draft.freq === 'once' && (
            <input
              type="datetime-local"
              style={{ width: 190 }}
              defaultValue={draft.at_local || ''}
              onInput={(e) => {
                draft.at_local = e.currentTarget.value
                draft.bad = null
              }}
            />
          )}
          {/* The backend has taken an interval for "hourly" all along
              (`every_seconds`); the form only ever sent the default, so every
              hourly job in the product runs exactly once an hour. */}
          {draft.freq === 'hour' && (
            <label className="everyn">
              {t('gui.job.every_n_pre')}
              <input
                type="number"
                min={1}
                max={24}
                defaultValue={String(Math.max(1, Math.round((draft.every_ms || 3600000) / 3600000)))}
                onInput={(e) => {
                  const n = Math.max(1, Math.min(24, Number(e.currentTarget.value) || 1))
                  draft.every_ms = n * 3600000
                  draft.bad = null
                }}
              />
              {t('gui.job.every_n_post')}
            </label>
          )}
          {draft.freq !== 'hour' && draft.freq !== 'once' && draft.freq !== 'week' && (
            <AtInput draft={draft} />
          )}
          {draft.freq === 'week' && (
            <input
              type="text"
              style={{ width: 90 }}
              defaultValue={draft.at}
              placeholder="09:30"
              onInput={(e) => {
                draft.at = e.currentTarget.value
                draft.bad = null
              }}
            />
          )}
        </div>
        {draft.bad && <span className="e">{t(draft.bad)}</span>}
      </div>
      {/* Where results go is a setting, not a property of one job: the save
          payload has never carried a per-job destination (`jobToSave` does not
          read it, and every row comes back as `app`), so the selector that
          stood here promised a choice the write threw away. It states the
          effective setting and points at the one place that can change it --
          the channels section, because what a result can be delivered TO is
          whichever channel is in service. */}
      <div className="ff">
        <label>{t('gui.job.deliver')}</label>
        <div className="sustate" style={{ marginTop: 0 }}>
          <span>{t(DELIVER[draft.deliver] || DELIVER.app!)}</span>
          <span className="x">{t('gui.job.deliver_global')}</span>
          <span className="a">
            <button className="mini ghost" onClick={() => settingsDialog.openSection('channels')}>
              {t('gui.job.deliver_open')}
            </button>
          </span>
        </div>
      </div>
    </>
  )
}

/* The daily/cron time field; for a raw expression the hint translates it
   live as the reader types, so nobody has to read five-field cron. */
function AtInput({ draft }: { draft: CronDraft }): JSX.Element {
  const [expr, setExpr] = useState(draft.at)
  return (
    <>
      <input
        type="text"
        style={{ width: 140 }}
        defaultValue={draft.at}
        placeholder={draft.freq === 'cron' ? '0 8 * * *' : '08:00'}
        onInput={(e) => {
          draft.at = e.currentTarget.value
          draft.bad = null
          setExpr(e.currentTarget.value)
        }}
      />
      {/* Not a standing hint -- the class those went out with. This is the
          expression read back in words as the reader types, which is the only
          way five cron fields are checkable without running them. */}
      {draft.freq === 'cron' && <span className="cronecho">{cronExprHuman(expr)}</span>}
    </>
  )
}

/* The new-job sheet, rendered into the static #jobVeil container the page
   markup keeps; the veil's own open flag and click-outside behaviour are
   all managed here. */
function JobSheet({ draft }: { draft: CronDraft }): JSX.Element | null {
  const veil = document.getElementById('jobVeil')
  const cancel = (): void => store.closeSheet()
  useEffect(() => {
    if (!veil) return
    veil.dataset.open = 'true'
    /* React's autoFocus does not reach a portal reliably; focus by hand. */
    veil.querySelector('input')?.focus()
    const onClick = (e: MouseEvent): void => {
      if (e.target === veil) cancel()
    }
    veil.addEventListener('click', onClick)
    return () => {
      veil.dataset.open = 'false'
      veil.removeEventListener('click', onClick)
    }
  }, [veil])
  if (!veil) return null
  const save = (): void => {
    if (!draft.name.trim() || !draft.what.trim()) {
      draft.blank = true
      store.redraw()
      return
    }
    store
      .source()
      .save(draft)
      .then((saved) => {
        store.closeSheet()
        toast(t('gui.job.saved_x', { name: saved.name }), {
          label: t('gui.job.run_once'),
          fn: () => void store.source().runNow(saved).then(() => store.refresh()),
        })
        void store.refresh()
      })
      .catch((e: unknown) => jobRefuse(draft, e))
  }
  return createPortal(
    <div
      className="sheet"
      role="dialog"
      aria-modal="true"
      aria-labelledby="jobTitle"
      style={{ width: 'min(560px,92vw)' }}
    >
      <header id="jobTitle">{t(draft.fresh ? 'gui.cron_new' : 'gui.job.edit_title')}</header>
      <div className="body" id="jobBody">
        <JobForm draft={draft} />
      </div>
      <footer>
        <button className="btn" id="jobNo" onClick={cancel}>
          {t('gui.cancel')}
        </button>
        <button className="btn key" id="jobYes" onClick={save}>
          {t('gui.save')}
        </button>
      </footer>
    </div>,
    veil,
  )
}
