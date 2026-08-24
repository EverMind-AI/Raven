import { useEffect, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { shell, t } from '../../shell/bridge'
import { show as menuAt } from '../../shell/menu'
import { show as toast } from '../../shell/toast'
import { cronExprHuman, cronWhen } from './humanize'
import * as store from './store'

import type { CronDraft, CronJob, CronRun } from './types'
import type { JSX } from 'react'

/* Mirrors the FREQ/DELIVER tables in ui/src/demo/030-fixtures.js: that copy
   feeds the fixture source's prose, this one feeds the form. The demo copy
   dies with the fixtures at the end of the migration. */
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
  shell().confirmAsk(
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
  const s = useSyncExternalStore(store.subscribe, store.getState)
  const job = s.viewId ? s.rows.find((x) => x.id === s.viewId) : undefined
  useEffect(() => {
    if (s.viewId && !job) store.backToList()
  }, [s.viewId, job])
  const draft = job && s.draft && s.draft.id === job.id ? s.draft : null
  return (
    <>
      {job && draft ? (
        <CronDetail key={`${job.id}:${s.epoch}`} job={job} draft={draft} rev={s.rev} lang={s.lang} />
      ) : s.loaded || s.rows.length ? (
        <CronList rows={s.rows} />
      ) : null}
      {s.sheet ? <JobSheet key={`sheet:${s.epoch}`} draft={s.sheet} /> : null}
    </>
  )
}

function CronList({ rows }: { rows: CronJob[] }): JSX.Element {
  const fail = rows.filter((j) => j.on && j.runs[0] && !j.runs[0].ok)
  return (
    <>
      <div className="herorow">
        <div className="pmhero">
          <h3>{t('gui.cron.hero')}</h3>
        </div>
        <button className="mini gold" onClick={() => store.openSheet()}>
          {t('gui.cron_new')}
        </button>
      </div>
      {fail.length > 0 && (
        <div className="banner" style={{ margin: '0 0 18px' }}>
          <b>{t('gui.cron.failing', { n: fail.length })}</b>
          <span>{fail.map((j) => j.name).join(', ')}</span>
          <button onClick={() => void store.source().openRun(fail[0]!, fail[0]!.runs[0])}>
            {t('gui.cron.why')}
          </button>
        </div>
      )}
      {rows.length === 0 ? (
        <div className="empty-note">{t('gui.cron.none')}</div>
      ) : (
        <div className="cronlist">
          {rows.map((j) => (
            <CronRow key={j.id} j={j} />
          ))}
        </div>
      )}
    </>
  )
}

function CronRow({ j }: { j: CronJob }): JSX.Element {
  const last = j.runs[0]
  return (
    <div
      className={'cronjob' + (j.on && last && !last.ok ? ' bad' : '')}
      style={{ cursor: 'pointer' }}
      onClick={(e) => {
        if ((e.target as HTMLElement).closest('button')) return
        store.openDetail(j)
      }}
    >
      <div className="nm">
        <span className={'dot' + (!j.on ? '' : last && !last.ok ? ' err' : ' run')} />
        <span>{j.name}</span>
        <span className="when">{j.when}</span>
      </div>
      <div className="mo">{j.on ? t('gui.cron.next_only', { next: j.next }) : t('gui.cron.paused')}</div>
      <div className="foot">
        {last ? (
          <>
            <button
              className={'mini ghost' + (!last.ok ? ' danger' : '')}
              onClick={() => void store.source().openRun(j, last)}
            >
              {t('gui.cron.last_run_short', {
                at: last.at,
                state: t(last.ok ? 'gui.cron.ok' : 'gui.cron.failed'),
              })}
            </button>
            <span
              className="mono"
              style={{
                fontSize: 11,
                color: 'var(--faint)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                minWidth: 0,
                flex: 1,
              }}
            >
              {last.note}
            </span>
          </>
        ) : (
          <span className="mono" style={{ fontSize: 11, color: 'var(--faint)' }}>
            {t('gui.cron.never')}
          </span>
        )}
      </div>
      <div className="ctl">
        <button
          className="swi"
          role="switch"
          aria-checked={j.on}
          aria-label={t('gui.caps.toggle_aria', { name: j.name })}
          onClick={() => void store.source().toggle(j).then(() => store.refresh())}
        />
        <button
          className="mini ghost"
          aria-label={t('gui.cron.menu_aria', { name: j.name })}
          onClick={(e) => {
            const b = e.currentTarget.getBoundingClientRect()
            menuAt(b.right - 150, b.bottom + 6, [
              { label: t('gui.cron.run_now'), fn: () => void store.source().runNow(j).then(() => store.refresh()) },
              { label: t('gui.cron.history'), fn: () => store.openDetail(j) },
              { label: t('gui.cron.open_session'), fn: () => void store.source().openRun(j) },
              '-',
              { label: t('gui.cron.delete'), bad: true, fn: () => removeThenList(j) },
            ])
          }}
        >
          ⋯
        </button>
      </div>
    </div>
  )
}

/* The job's own page: header actions, the editable schedule, and every run
   it has made -- each row opens the transcript that run wrote. */
function CronDetail({ job, draft, rev, lang }: { job: CronJob; draft: CronDraft; rev: number; lang: number }): JSX.Element {
  const [runs, setRuns] = useState<CronRun[] | null>(null)
  /* Keyed on rev, not just the job: the legacy page refetched history on
     every draw, and the shell leans on that -- live's cron.finished handler
     calls refresh() precisely so a run that lands while the reader is on
     this page drops into the card. */
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
  return (
    <>
      <div className="pmback">
        <button className="mini ghost" onClick={() => store.backToList()}>
          {'← ' + t('gui.cron.back')}
        </button>
        <div className="trow">
          <b>{job.name}</b>
          <div className="cdacts">
            <button className="mini gold" onClick={() => void store.source().runNow(job).then(() => store.refresh())}>
              {t('gui.cron.run_now')}
            </button>
            <button className="mini ghost danger" onClick={() => removeThenList(job)}>
              {t('gui.cron.delete')}
            </button>
            <button
              className="swi"
              role="switch"
              aria-checked={job.on}
              aria-label={t('gui.caps.toggle_aria', { name: job.name })}
              onClick={() => void store.source().toggle(job).then(() => store.refresh())}
            />
          </div>
        </div>
        <div className="cdstat">
          <span className={'dot' + (job.on ? ' on' : '')} />
          <span>
            {job.on
              ? t('gui.cron.next', { when: cronWhen(job), next: job.next })
              : `${cronWhen(job)} · ${t('gui.cron.paused')}`}
          </span>
        </div>
      </div>
      <div className="cdcard">
        <div className="t">{t('gui.cron.cfg')}</div>
        <div>
          <JobForm draft={draft} />
        </div>
        <div className="cdfoot">
          <button className="mini gold" onClick={save}>
            {t('gui.cron.save')}
          </button>
        </div>
      </div>
      <div className="cdcard">
        <div className="t">
          {t('gui.cron.hist_title2')}
          {runs !== null && runs.length > 0 && <span className="n">{String(runs.length)}</span>}
        </div>
        <div className="cdruns">
          {runs === null ? null : runs.length === 0 ? (
            <div className="empty-note">{t('gui.cron.hist_none')}</div>
          ) : (
            runs.map((run, i) => (
              <button key={i} className="cdrun" onClick={() => void store.source().openRun(job, run)}>
                <span className={'st' + (run.ok ? ' ok' : ' bad')} />
                <span className="at">{run.at}</span>
                <span className="note">{run.note || ''}</span>
                <span className="chev">›</span>
              </button>
            ))
          )}
        </div>
      </div>
    </>
  )
}

/* create / edit -- one form, two hosts: the new-job sheet and the detail
   page's config card both edit the same draft shape.

   Inputs are uncontrolled on purpose, mirroring the legacy form: a keystroke
   mutates the draft object and re-renders nothing, so focus and IME
   composition survive; only a frequency change or a refusal redraws. The
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
        {blankName && <span className="hint">{t('gui.job.need_name')}</span>}
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
        {blankWhat && <span className="hint">{t('gui.job.need_what')}</span>}
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
        {draft.bad && <span className="hint">{t(draft.bad)}</span>}
      </div>
      <div className="ff">
        <label>{t('gui.job.deliver')}</label>
        <select
          defaultValue={draft.deliver}
          onChange={(e) => {
            draft.deliver = e.currentTarget.value
          }}
        >
          {Object.entries(DELIVER).map(([k, v]) => (
            <option key={k} value={k}>
              {t(v)}
            </option>
          ))}
        </select>
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
      {draft.freq === 'cron' && <span className="hint">{cronExprHuman(expr)}</span>}
    </>
  )
}

/* The new-job sheet, rendered into the static #jobVeil container the page
   markup keeps; the veil's own open flag and click-outside behaviour are
   managed here because nothing legacy owns them any more. */
function JobSheet({ draft }: { draft: CronDraft }): JSX.Element | null {
  const veil = document.getElementById('jobVeil')
  const cancel = (): void => store.closeSheet()
  useEffect(() => {
    if (!veil) return
    veil.dataset.open = 'true'
    /* React's autoFocus does not reach a portal reliably; focus by hand,
       the way the legacy sheet did on open. */
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
