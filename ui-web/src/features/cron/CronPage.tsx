import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import {
  TwoPane, TwoPaneFind, TwoPaneFoot, TwoPaneGroup, TwoPaneHead, TwoPaneList, TwoPaneNoHit, TwoPaneNone, TwoPaneRow,
  TwoPaneSection, TwoPaneSwitch, TwoPaneWait,
} from '../../components/TwoPane'
import { t } from '../../i18n/t'
import { ask as confirmAsk } from '../../state/confirm'
import * as lang from '../../state/lang'
import { show as toast } from '../../state/toast'
import { cronExprHuman, cronWhen } from './humanize'
import * as store from './store'
import './styles.css'

import type { CronDraft, CronJob, CronRun } from './types'
import type { JSX } from 'react'

/* The frequencies and the delivery routes the editor offers. Page data, not
   wire data: a job's own kind and expression come from `cron.list`, and these
   are the choices the form can express them as.

   "Every N hours" is folded into this list rather than standing as a number box
   beside it. The box was a control a reader had to notice to use, so in
   practice every hourly job in the product ran exactly once an hour. */
const FREQ: Array<{ id: string; label: string; vars?: Record<string, unknown> }> = [
  { id: 'hour:1', label: 'gui.freq.hour' },
  { id: 'hour:2', label: 'gui.cron.h.every_h', vars: { n: 2 } },
  { id: 'hour:3', label: 'gui.cron.h.every_h', vars: { n: 3 } },
  { id: 'hour:4', label: 'gui.cron.h.every_h', vars: { n: 4 } },
  { id: 'hour:6', label: 'gui.cron.h.every_h', vars: { n: 6 } },
  { id: 'hour:12', label: 'gui.cron.h.every_h', vars: { n: 12 } },
  { id: 'day', label: 'gui.freq.day' },
  { id: 'week', label: 'gui.freq.week' },
  { id: 'month', label: 'gui.freq.month' },
  { id: 'once', label: 'gui.freq.once' },
  { id: 'cron', label: 'gui.freq.cron' },
]
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

const CLOCK_GLYPH = (
  <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></svg>
)

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
  /* No jobs and none being drafted: one column, and no form to fill in -- a
     job is something to ask Raven for in a conversation, so that is the whole
     of what the empty state says. A search over nothing and a "+" beside it
     were two halves of a page with nothing in it. */
  if (s.loaded && !s.rows.length && !(s.sheet && !s.parked)) {
    return (
      <TwoPane>
        <TwoPaneNone icon={CLOCK_GLYPH} title={t('gui.cron.none_t')}>{t('gui.cron.none')}</TwoPaneNone>
      </TwoPane>
    )
  }
  return (
    <TwoPane side={<CronSide rows={s.rows} loaded={s.loaded} q={q} onQ={setQ} viewId={s.viewId} />}>
      {/* A job being created stands in the right column, where a job being
          edited stands. It used to be a modal over this dialog, which is a
          layer over a layer -- and it covered the list a reader was about to
          name the new job against. */}
      {s.sheet && !s.parked ? (
        <CronNew key={`new:${s.epoch}`} draft={s.sheet} />
      ) : job && draft ? (
        <CronDetail key={`${job.id}:${s.epoch}`} job={job} draft={draft} rev={s.rev} lang={pageLang} />
      ) : (
        <TwoPaneNone>{t('gui.cron.pick')}</TwoPaneNone>
      )}
    </TwoPane>
  )
}

/* A job that does not exist yet: the same four blocks, with no switch to throw
   and no history to show, and its own two verbs at the foot. Nothing is
   written until "create" -- leaving a box commits an existing job, and there
   is nothing here to commit to. */
function CronNew({ draft }: { draft: CronDraft }): JSX.Element {
  const create = (): void => {
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
        store.viewSaved(saved)
        toast(t('gui.job.saved_x', { name: saved.name }), {
          label: t('gui.job.run_once'),
          fn: () => void store.source().runNow(saved).then(() => store.refresh()),
        })
      })
      .catch((e: unknown) => jobRefuse(draft, e))
  }
  return (
    <>
      <TwoPaneHead
        name={draft.name.trim() || t('gui.cron_new')}
        meta={<span className="st">{draftWords(draft)}</span>}
      />
      <JobForm draft={draft} />
      <TwoPaneFoot>
        <button className="mini go" onClick={create}>{t('gui.cron.create')}</button>
        <button className="mini ghost" onClick={() => store.closeSheet()}>{t('gui.cancel')}</button>
      </TwoPaneFoot>
    </>
  )
}

/* A draft's schedule in words, for the head of a job that has none yet: a
   saved row carries the sentence the source worded, and this is the same
   sentence read off the controls instead. No claim about a first run -- what
   instant a schedule next falls on is the server's answer, and guessing it
   here would be a second clock to disagree with. */
function draftWords(draft: CronDraft): string {
  const hm = draft.at || '09:00'
  if (draft.freq === 'once') {
    return draft.at_local ? draft.at_local.replace('T', ' ') : t('gui.cron.no_time')
  }
  if (draft.freq === 'cron') return cronExprHuman(draft.at)
  if (draft.freq === 'week') return `${t('gui.cron.h.dow' + String(draft.wd ?? 1))} ${hm}`
  if (draft.freq === 'month') return t('gui.cron.h.monthly', { d: draft.dom ?? 1, hm })
  if (draft.freq === 'day') return `${t('gui.cron.h.daily')} ${hm}`
  return freqWords(draft)
}

function freqWords(draft: CronDraft): string {
  const hit = FREQ.find((f) => f.id === freqValue(draft))
  return hit ? t(hit.label, hit.vars ?? null) : ''
}

const failing = (j: CronJob): boolean => j.on && !!j.runs[0] && !j.runs[0].ok

/* The left column: the search, the button that adds one, and the jobs in two
   runs -- the ones that are on, then the ones that are not. Two groups rather
   than three filter chips: what a reader arrives asking is "did anything
   break", and a job whose last run failed says so on its own second line, in
   the colour, where a chip could only ever say how many. */
/* The run history before it is in: the rows it becomes, at their own height,
   with a bar where the stamp and the note will be. */
function RunsWait(): JSX.Element {
  return (
    <div className="cronwait" role="status" aria-busy="true" aria-label={t('gui.cron.reading')}>
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="cronrun">
          <i />
          <span className="cronwbar" style={{ width: '104px' }} />
          <span className="cronwbar" style={{ width: `${34 + ((i * 19) % 30)}%` }} />
        </div>
      ))}
    </div>
  )
}

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
        sub={
          broke && last
            ? `${t('gui.cron.failed')} · ${last.at}`
            : cronWhen(j)
        }
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
        {!loaded && !rows.length ? <TwoPaneWait /> : shown.length === 0 ? (
          rows.length ? <TwoPaneNoHit>{t('gui.cron.f_none')}</TwoPaneNoHit> : null
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

/* The job itself, in the right column: name, how often, what to say, and what
   it has done -- four labelled blocks in one scroll, the way every other
   section of this panel reads.
 *
 * It saves as you leave a box rather than on a button. A schedule is a setting,
 * not a document: there is nothing to draft here, and a "save changes" button
 * on a four-field form is one more press between the reader and a job that
 * already says what they meant. The native `change` moment is what commits --
 * a select the moment it is picked, a box when the reader leaves it -- so a
 * half-typed expression is never written, and a refusal stands under the
 * control that has to be fixed.
 *
 * What acts on the whole job -- run it now, open its conversation, delete it --
 * is at the foot, under a hairline, away from the boxes. */
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
          <TwoPaneSwitch
            on={job.on}
            label={t('gui.caps.toggle_aria', { name: job.name })}
            onChange={() => void store.source().toggle(job).then(() => store.refresh())}
          />
        }
      />
      <JobForm draft={draft} onCommit={commitDraft} />
      <TwoPaneSection
        label={
          <>
            {t('gui.cron.tab_runs')}
            {runs && runs.length > 5 ? (
              <span className="two-pane-sub">{t('gui.cron.runs_n', { n: runs.length })}</span>
            ) : null}
          </>
        }
      >
        <div className="cronruns">
          {/* The history is fetched when the job opens, and drawing nothing
              until it lands said the same thing as "it has never run". */}
          {runs === null ? <RunsWait /> : runs.length === 0 ? (
            <div className="dnote">{t('gui.cron.hist_none')}</div>
          ) : (
            runs.map((run, i) => (
              <button
                key={i}
                className={run.ok ? 'cronrun' : 'cronrun cronbad'}
                onClick={() => void store.source().openRun(job, run)}
              >
                <i />
                <span className="cronat">{run.at}</span>
                <span className="cronnote">{run.note || t(run.ok ? 'gui.cron.ok' : 'gui.cron.failed')}</span>
              </button>
            ))
          )}
        </div>
      </TwoPaneSection>
      <TwoPaneFoot>
        <button className="mini" disabled={running} onClick={runNow}>
          {t(running ? 'gui.cron.running_now' : 'gui.cron.run_now')}
        </button>
        <button className="mini ghost" onClick={() => void store.source().openRun(job)}>
          {t('gui.cron.open_sess')}
        </button>
        <span className="two-pane-sp" />
        <button className="mini ghost danger" onClick={() => removeThenList(job)}>
          {t('gui.cron.delete')}
        </button>
      </TwoPaneFoot>
    </>
  )
}

/* Writing a draft the reader has finished with. The validation is the same one
   the create sheet runs, so a name or an instruction left empty is refused
   here too rather than written as a blank job.

   The answer is not allowed to move the reader or rebuild their form. A blur
   is what starts this, so by the time it lands the reader has already gone
   somewhere -- the next box, or another job they picked out of the list -- and
   selecting the saved row would undo that. The draft it carries is the
   request's own, too, so putting it back drops whatever was typed after the
   blur, and the epoch that replaces a draft remounts the whole detail. The
   rows are enough: the head, the list and the next-run line all read from
   them, and whichever job is open now keeps the draft it has. */
function commitDraft(draft: CronDraft): void {
  if (!draft.name.trim() || !draft.what.trim()) {
    draft.blank = true
    store.redraw()
    return
  }
  store
    .source()
    .save(draft)
    .then(() => {
      void store.refresh()
      toast(t('gui.cron.saved'))
    })
    .catch((e: unknown) => jobRefuse(draft, e))
}

/* create / edit -- one form, two hosts: the new-job sheet and the detail
   page's own blocks both edit the same draft shape.

   Inputs are uncontrolled on purpose: a keystroke mutates the draft object and
   re-renders nothing, so focus and IME composition survive; only a frequency
   change or a refusal redraws. The store's epoch key remounts this subtree
   whenever a draft is replaced.

   `onCommit` is what the detail hands in to write on the way out of a box. The
   sheet hands in nothing: a job that does not exist yet is created by its own
   button, not by leaving a field. */
function JobForm({ draft, onCommit }: { draft: CronDraft; onCommit?(d: CronDraft): void }): JSX.Element {
  if (draft.freq === 'week' && draft.wd == null) draft.wd = 1
  if (draft.freq === 'month' && draft.dom == null) draft.dom = 1
  const blankName = Boolean(draft.blank) && !draft.name.trim()
  const blankWhat = Boolean(draft.blank) && !draft.what.trim()
  /* What the boxes held when this form mounted. The draft itself is mutated by
     every keystroke, so it cannot answer "did this change"; the snapshot can,
     and the store's epoch remounts this subtree whenever a draft is replaced. */
  const seen = useRef({ name: draft.name, what: draft.what, at: draft.at, at_local: draft.at_local ?? '' })
  /* Committing on the way out of a box, and only where something changed: a
     reader who tabs through a form they did not touch has written nothing, and
     a toast saying "saved" on every blur is a lie four times over. */
  const commit = (field: keyof typeof seen.current, now: string): void => {
    if (!onCommit || seen.current[field] === now) return
    seen.current[field] = now
    onCommit(draft)
  }
  const pick = (v: string): void => {
    setFreq(draft, v)
    store.redraw()
    if (onCommit) onCommit(draft)
  }
  return (
    <>
      <TwoPaneSection label={t('gui.job.name')}>
        <input
          type="text"
          className="cronname"
          defaultValue={draft.name}
          placeholder={t('gui.job.name_ph')}
          data-bad={blankName ? 'true' : undefined}
          onInput={(e) => {
            draft.name = e.currentTarget.value
            draft.blank = null
          }}
          onBlur={(e) => commit('name', e.currentTarget.value)}
        />
        {blankName && <span className="cronerr">{t('gui.job.need_name')}</span>}
      </TwoPaneSection>
      <TwoPaneSection label={t('gui.job.freq')}>
        <div className="cronwhen" data-bad={draft.bad ? 'true' : undefined}>
          <span className="selw">
            <select className="sel" style={{ minWidth: 0, width: 128 }} value={freqValue(draft)}
              aria-label={t('gui.job.freq')} onChange={(e) => pick(e.currentTarget.value)}>
              {freqOptions(draft).map((o) => (
                <option key={o.id} value={o.id}>{o.label}</option>
              ))}
            </select>
          </span>
          {draft.freq === 'week' && (
            <NumSel width={92} label={t('gui.freq.week')} value={String(draft.wd ?? 1)}
              opts={[0, 1, 2, 3, 4, 5, 6].map((d) => [String(d), t('gui.cron.h.dow' + d)])}
              onPick={(v) => { draft.wd = Number(v); draft.bad = null; if (onCommit) onCommit(draft) }} />
          )}
          {draft.freq === 'month' && (
            <NumSel width={82} label={t('gui.freq.month')} value={String(draft.dom ?? 1)}
              opts={Array.from({ length: 31 }, (_, i) => [String(i + 1), t('gui.cron.u_dom', { n: i + 1 })])}
              onPick={(v) => { draft.dom = Number(v); draft.bad = null; if (onCommit) onCommit(draft) }} />
          )}
          {/* The clock, as two dropdowns rather than a native time input: that
              control is 12-hour with an AM/PM and a clock glyph in an English
              locale, which is not the same control as the ones beside it -- and
              a free-text "08:00" box refused half of what a reader typed. */}
          {(draft.freq === 'day' || draft.freq === 'week' || draft.freq === 'month') && (
            <HmSel draft={draft} onPick={() => { if (onCommit) onCommit(draft) }} />
          )}
          {draft.freq === 'once' && (
            <input
              type="datetime-local"
              style={{ width: 190 }}
              defaultValue={draft.at_local || ''}
              aria-label={t('gui.freq.once')}
              onInput={(e) => {
                draft.at_local = e.currentTarget.value
                draft.bad = null
              }}
              onBlur={(e) => commit('at_local', e.currentTarget.value)}
            />
          )}
          {draft.freq === 'cron' && <ExprInput draft={draft} onCommit={onCommit} seen={seen.current} />}
        </div>
        {draft.bad && <span className="cronerr">{t(draft.bad)}</span>}
      </TwoPaneSection>
      <TwoPaneSection label={t('gui.job.what')}>
        <textarea
          className="cronsay"
          rows={sayRows(draft.what)}
          defaultValue={draft.what}
          placeholder={t('gui.job.what_ph')}
          data-bad={blankWhat ? 'true' : undefined}
          onInput={(e) => {
            draft.what = e.currentTarget.value
            draft.blank = null
          }}
          onBlur={(e) => commit('what', e.currentTarget.value)}
        />
        {blankWhat && <span className="cronerr">{t('gui.job.need_what')}</span>}
      </TwoPaneSection>
    </>
  )
}

/* One dropdown, drawn the way every dropdown on this page is. */
function NumSel({ width, label, value, opts, onPick }: {
  width: number
  label: string
  value: string
  opts: Array<[string, string]>
  onPick(v: string): void
}): JSX.Element {
  return (
    <span className="selw">
      <select className="sel" style={{ minWidth: 0, width }} value={value} aria-label={label}
        onChange={(e) => { onPick(e.currentTarget.value); store.redraw() }}>
        {opts.map(([v, lab]) => <option key={v} value={v}>{lab}</option>)}
      </select>
    </span>
  )
}

const MINS = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]

/* The hour and the minute. A minute the schedule is already set to that is not
   on the five-minute grid stays on the list rather than being rounded away by
   the reader opening the page. */
function HmSel({ draft, onPick }: { draft: CronDraft; onPick(): void }): JSX.Element {
  const [h, m] = splitHm(draft.at)
  const mins = MINS.includes(m) ? MINS : [...MINS, m].sort((a, b) => a - b)
  const set = (hh: number, mm: number): void => {
    draft.at = `${pad2(hh)}:${pad2(mm)}`
    draft.bad = null
    onPick()
  }
  return (
    <>
      <NumSel width={82} label={t('gui.cron.u_hour', { n: h })} value={String(h)}
        opts={Array.from({ length: 24 }, (_, i) => [String(i), t('gui.cron.u_hour', { n: pad2(i) })])}
        onPick={(v) => set(Number(v), m)} />
      <NumSel width={82} label={t('gui.cron.u_min', { n: m })} value={String(m)}
        opts={mins.map((v) => [String(v), t('gui.cron.u_min', { n: pad2(v) })])}
        onPick={(v) => set(h, Number(v))} />
    </>
  )
}

/* A raw expression, with the words it means under it as the reader types. */
function ExprInput({ draft, onCommit, seen }: {
  draft: CronDraft
  onCommit?(d: CronDraft): void
  seen: { at: string }
}): JSX.Element {
  const [expr, setExpr] = useState(draft.at)
  return (
    <>
      <input
        type="text"
        style={{ width: 140 }}
        defaultValue={draft.at}
        placeholder="0 8 * * *"
        aria-label={t('gui.cron.expr_lab')}
        onInput={(e) => {
          draft.at = e.currentTarget.value
          draft.bad = null
          setExpr(e.currentTarget.value)
        }}
        onBlur={(e) => {
          if (!onCommit || seen.at === e.currentTarget.value) return
          seen.at = e.currentTarget.value
          onCommit(draft)
        }}
      />
      <span className="cronecho">{cronExprHuman(expr)}</span>
    </>
  )
}

const pad2 = (n: number): string => String(n).padStart(2, '0')

function splitHm(at: string): [number, number] {
  const m = /^\s*(\d{1,2}):(\d{2})\s*$/.exec(at || '')
  if (!m) return [9, 0]
  return [Math.min(23, Number(m[1])), Math.min(59, Number(m[2]))]
}

/* A one-sentence instruction should not stand in a five-line box, and a long
   one should not be read through a four-line window. */
function sayRows(what: string): number {
  const rows = (what.match(/\n/g) || []).length + Math.ceil(what.length / 72) + 1
  return Math.max(4, Math.min(14, rows))
}

/* Which of the dropdown's entries a draft is on. */
function freqValue(draft: CronDraft): string {
  if (draft.freq !== 'hour') return draft.freq
  return 'hour:' + String(Math.max(1, Math.round((draft.every_ms || 3600000) / 3600000)))
}

/* The entries, plus the one a draft is on when it is not among them: a job set
   up in a conversation can run every 30 minutes, which none of the ten says,
   and dropping it would rewrite the schedule the moment the page opened. */
function freqOptions(draft: CronDraft): Array<{ id: string; label: string }> {
  const out = FREQ.map((f) => ({ id: f.id, label: t(f.label, f.vars ?? null) }))
  const cur = freqValue(draft)
  if (!out.some((o) => o.id === cur)) out.unshift({ id: cur, label: cronWhen(draft) })
  return out
}

/* A pick, written into the draft. An hourly interval rides on the entry rather
   than on a number box beside it. */
function setFreq(draft: CronDraft, v: string): void {
  const [kind, n] = v.split(':')
  draft.freq = kind as CronJob['freq']
  draft.bad = null
  if (kind === 'hour') draft.every_ms = (Number(n) || 1) * 3600000
  if (!draft.at) draft.at = '09:00'
}
