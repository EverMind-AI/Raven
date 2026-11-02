/* The tasks tab as the host's Task Board: one row per Task Group, newest first;
 * a row opens its thread in place, under itself; finished groups fold away at
 * the bottom; Tasks no step places sit in an Unassigned fold at the top.
 *
 * A conversation whose board holds no group -- one from before the board, or
 * one on a runtime that keeps none -- draws the flat list it always had, so
 * nothing about such a conversation changes. That list is the tasks page's own
 * and is handed in (`flat`), which keeps this module from importing the page
 * that renders it; so is the verb that opens a Task's pane (`openTask`), the
 * desk's to perform.
 */

import { ArrowRight01Icon, ArrowRight02Icon, ArrowTurnBackwardIcon, Tick02Icon } from '@hugeicons/core-free-icons'
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { ago } from '../../lib/ago'
import { current as sessionCurrent } from '../../lib/session'
import { revealTurnAt } from '../transcript/source'
import { CATEGORY_WORD, categoryOf } from './boards'
import * as store from './store'

import type { LinkedRow } from './store'
import type { ProgressStep, TaskGroup, TaskRow } from './types'
import type { JSX } from 'react'

const msOf = (iso: string): number => {
  const v = new Date(iso).getTime()
  return Number.isNaN(v) ? 0 : v
}

const pad = (n: number): string => String(n).padStart(2, '0')

/* `HH:MM` for a step taken today, the date before it otherwise. */
function stepTime(iso: string, now: number): string {
  const d = new Date(msOf(iso))
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return d.toDateString() === new Date(now).toDateString() ? hm : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`
}

/* What the row says at its right: a running graph's progress, or how many
   sub-agents are running for the group, or else how long ago it last moved. */
function metaOf(group: TaskGroup, rows: TaskRow[], lineage: Record<string, string>, now: number): string {
  const running = group.steps
    .flatMap((s) => store.linkedRows(s, rows, lineage))
    .map((l) => l.row)
    .filter((r): r is TaskRow => r !== null && r.status === 'running')
  const dag = running.find((r) => r.kind === 'dag')
  if (dag) return t('gui.tasks.board.dag', { done: dag.counts.completed, total: dag.counts.total })
  if (running.length) return t('gui.tasks.board.running', { n: running.length })
  return ago(group.updated_at, now)
}

type OpenTask = (row: TaskRow) => void

/* Every Task a group's steps place, a continued run's successors included,
   each once however many steps name it. */
function groupLinks(group: TaskGroup, rows: TaskRow[], lineage: Record<string, string>): LinkedRow[] {
  const seen = new Map<string, LinkedRow>()
  for (const step of group.steps) {
    for (const linked of store.linkedRows(step, rows, lineage)) seen.set(`${linked.link.kind}:${linked.link.id}`, linked)
  }
  return [...seen.values()]
}

/* A spawn is one sub-agent and a graph run is each of its nodes, as the Task
   Board page counts them; a Task the list no longer holds still counts once. */
function subAgentsOf(links: LinkedRow[]): number {
  return links.reduce((n, l) => n + (l.row?.kind === 'dag' ? l.row.counts.total || 1 : 1), 0)
}

/* The group's type in words, the Task Board page's four and its rule. */
function Pill({ group, running }: { group: TaskGroup; running: boolean }): JSX.Element {
  const type = categoryOf(group.status, running)
  const mark = type === 'running' ? 'tkpillring' : 'tkpilldot'
  return (
    <span className="tkpill" data-tone={type}>
      <span className={mark} aria-hidden="true" />
      {t(CATEGORY_WORD[type])}
    </span>
  )
}

/* An event is in progress while a Task it started still runs; one that left a
   failure behind reads the way the tab marks a failed Task; every other event
   is done -- including one that started nothing. */
type EventState = 'run' | 'error' | 'ok'

function eventState(linked: LinkedRow[]): EventState {
  const statuses = linked.flatMap((l) => (l.row ? [l.row.status] : []))
  if (statuses.includes('running')) return 'run'
  return statuses.some((s) => s === 'failed' || s === 'interrupted') ? 'error' : 'ok'
}

const EVENT_WORD: Record<EventState, string> = {
  run: 'gui.tasks.running',
  error: 'gui.tasks.st_failed',
  ok: 'gui.tasks.st_completed',
}

function Chip({ linked, openTask }: { linked: LinkedRow; openTask: OpenTask }): JSX.Element {
  const { link, row } = linked
  const what = row?.task_summary || link.summary || link.id
  const label = <><span className="tkchipk">{link.kind}</span>{` · ${what}`}</>
  /* A Task `tasks.list` no longer holds -- a branched conversation, a removed
     run -- is still named, but there is no pane to open. */
  if (!row) return <span className="tkchip" data-gone="1" title={`${link.kind} · ${what}`}>{label}</span>
  return (
    <button type="button" className="tkchip" data-st={row.status} onClick={() => openTask(row)}>
      {label}
    </button>
  )
}

async function setStatus(group: TaskGroup, status: 'completed' | 'idle'): Promise<void> {
  const src = store.source()
  const key = sessionCurrent()
  if (!src?.setGroupStatus || !key) return
  const got = await src.setGroupStatus(key, group.id, status)
  store.onBoardUpdated({ session_key: key, board: got.board, lineage: got.lineage })
}

async function setAside(group: TaskGroup, step: ProgressStep, question: string): Promise<void> {
  const src = store.source()
  const key = sessionCurrent()
  if (!src?.ignoreQuestion || !key) return
  const got = await src.ignoreQuestion(key, group.id, step.id, question)
  store.onBoardUpdated({ session_key: key, board: got.board, lineage: got.lineage })
}

/* What a thread's controls do. The sidebar's act on the conversation on
   screen; the Task Board page's on the card's own, which need not be. A thread
   offers Move on only where its card has no foot to carry it. */
export interface ThreadVerbs {
  readonly reveal: (step: ProgressStep) => void
  readonly setStatus: (group: TaskGroup, status: 'completed' | 'idle') => void
  readonly ignore: (group: TaskGroup, step: ProgressStep, question: string) => void
  readonly answer: (group: TaskGroup) => void
  readonly moveOn?: (group: TaskGroup) => void
}

const HERE: ThreadVerbs = {
  reveal: (step) => { revealTurnAt(step.turn_at, step.turn_id) },
  setStatus: (group, status) => { void setStatus(group, status) },
  ignore: (group, step, question) => { void setAside(group, step, question) },
  answer: (group) => store.answer(group),
  moveOn: (group) => {
    const key = sessionCurrent()
    if (key) store.moveOn(key, group)
  },
}

/* Move on: the reader's next message is about this group -- drawn above the
   composer, and told to the server as that message goes. */
export function MoveOnButton({ onClick }: { onClick: () => void }): JSX.Element {
  return (
    <button type="button" className="tkmove" onClick={onClick}>
      {t('gui.tasks.board.move_on')}
      <Icon icon={ArrowRight02Icon} size={12} stroke={2} />
    </button>
  )
}

/* While a group needs the reader: the questions its latest event asked, at the
   bottom of the card -- under the head while it is folded, after that event
   while it is open -- each in a box of its own with an Ignore that sets that
   one aside, and Answer, which drafts a reply to the rest in the composer. A
   group that asks nothing it named keeps one Ignore, which puts it down as idle. */
function Questions({ group, verbs }: { group: TaskGroup; verbs: ThreadVerbs }): JSX.Element | null {
  const step = group.steps[group.steps.length - 1]
  const questions = step?.questions ?? []
  if (!step || !questions.length) return null
  return (
    <ol className="tkasklist" aria-label={t('gui.tasks.board.questions')}>
      {questions.map((q, n) => (
        <li key={`${n}:${q}`} className="tkq">
          <span className="tkqtext">{q}</span>
          <button
            type="button"
            className="tkqignore"
            aria-label={t('gui.tasks.board.ignore_question', { n: n + 1 })}
            onClick={() => verbs.ignore(group, step, q)}
          >
            {t('gui.tasks.board.ignore')}
          </button>
        </li>
      ))}
    </ol>
  )
}

function AskButtons({ group, verbs }: { group: TaskGroup; verbs: ThreadVerbs }): JSX.Element {
  const named = (group.steps[group.steps.length - 1]?.questions ?? []).length > 0
  return (
    <>
      {named ? null : (
        <button type="button" className="tkaskbtn" data-kind="ignore" onClick={() => verbs.setStatus(group, 'idle')}>
          {t('gui.tasks.board.ignore')}
        </button>
      )}
      <button type="button" className="tkaskbtn" data-kind="answer" onClick={() => verbs.answer(group)}>
        {t('gui.tasks.board.answer')}
      </button>
    </>
  )
}

function Ask({ group }: { group: TaskGroup }): JSX.Element {
  return (
    <div className="tkask">
      <Questions group={group} verbs={HERE} />
      <div className="tkaskacts"><AskButtons group={group} verbs={HERE} /></div>
    </div>
  )
}

/* A group's events in time order, each opening the reply of its turn, then the
   questions it waits on and the group's own buttons. Its Tasks are named only
   where a Task's pane can be opened (`openTask`); they still mark each event. */
export function Thread({ id, group, linked, now, verbs, openTask }: {
  id: string; group: TaskGroup; linked: (step: ProgressStep) => LinkedRow[]; now: number
  verbs: ThreadVerbs; openTask?: OpenTask
}): JSX.Element {
  const asking = group.status === 'needs_you'
  return (
    <div className="tkthread" id={id}>
      <ul className="tksteps">
        {group.steps.map((step, i) => {
          const links = linked(step)
          const state = eventState(links)
          /* The event that left the group needing the user says, in its
             summary, what it asks -- shown while the group still waits. */
          const asks = group.status === 'needs_you' && i === group.steps.length - 1
          return (
            <li key={step.id} className="tkstep" data-st={state} data-asks={asks || undefined}>
              <span className="tkmark" aria-hidden="true">
                {state === 'ok' ? <Icon icon={Tick02Icon} size={9} stroke={3} /> : state === 'error' ? '!' : null}
              </span>
              <span className="tksr">{t(EVENT_WORD[state])}</span>
              <button type="button" className="tksteptext" onClick={() => verbs.reveal(step)}>
                <span className="tkstept">{step.title}</span>
                {asks && step.summary ? <span className="tkstepsum"><span>{step.summary}</span></span> : null}
              </button>
              <div className="tkstepmeta">
                <time>{stepTime(step.turn_at, now)}</time>
                {openTask ? links.map((l) => <Chip key={`${l.link.kind}:${l.link.id}`} linked={l} openTask={openTask} />) : null}
              </div>
            </li>
          )
        })}
      </ul>
      {asking ? <Questions group={group} verbs={verbs} /> : null}
      <div className="tkthreadacts">
        <button
          type="button"
          className="tkgroupact"
          onClick={() => verbs.setStatus(group, group.status === 'completed' ? 'idle' : 'completed')}
        >
          <Icon icon={group.status === 'completed' ? ArrowTurnBackwardIcon : Tick02Icon} size={13} stroke={2} />
          <span>{group.status === 'completed' ? t('gui.tasks.board.reopen') : t('gui.tasks.board.mark_completed')}</span>
        </button>
        {verbs.moveOn ? <MoveOnButton onClick={() => verbs.moveOn?.(group)} /> : null}
        {asking ? <span className="tkaskpair"><AskButtons group={group} verbs={verbs} /></span> : null}
      </div>
    </div>
  )
}

function GroupRows({ groups, rows, lineage, now, openIds, onToggle, done, openTask }: {
  groups: TaskGroup[]; rows: TaskRow[]; lineage: Record<string, string>; now: number
  openIds: Set<string>; onToggle: (id: string) => void; done: boolean; openTask: OpenTask
}): JSX.Element {
  return (
    <>
      {groups.map((group) => {
        const open = openIds.has(group.id)
        const links = groupLinks(group, rows, lineage)
        const running = links.some((l) => l.row?.status === 'running')
        return (
          <div key={group.id} className="tkgcard" data-open={open || undefined} data-done={done || undefined}>
            <button
              type="button"
              className={'tkgroup' + (done ? ' tkdone' : '')}
              data-group={group.id}
              data-st={group.status}
              aria-expanded={open}
              aria-controls={`tkthread-${group.id}`}
              onClick={() => onToggle(group.id)}
            >
              <span className="tkghead">
                <Pill group={group} running={running} />
                <span className="tkgmeta">{metaOf(group, rows, lineage, now)}</span>
              </span>
              <span className="nm">{group.title}</span>
              {group.summary ? <span className="st">{group.summary}</span> : null}
            </button>
            {/* Outside the head, so Move on is a button of its own rather
                than a press that also folds the card. */}
            {open ? null : (
              <div className="tkgfoot">
                <span>{t('gui.tasks.page.events', { n: group.steps.length })}</span>
                <span className="tkgsep" aria-hidden="true" />
                <span>{t('gui.tasks.page.sub_agents', { n: subAgentsOf(links) })}</span>
                <MoveOnButton onClick={() => HERE.moveOn?.(group)} />
              </div>
            )}
            {group.status === 'needs_you' && !open ? <Ask group={group} /> : null}
            {open ? (
              <Thread
                id={`tkthread-${group.id}`} group={group} now={now} verbs={HERE} openTask={openTask}
                linked={(step) => store.linkedRows(step, rows, lineage)}
              />
            ) : null}
          </div>
        )
      })}
    </>
  )
}

export function BoardList({ flat, openTask }: {
  flat: (rows: TaskRow[]) => JSX.Element
  openTask: OpenTask
}): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const [openIds, setOpenIds] = useState<Set<string>>(() => new Set())
  const [showDone, setShowDone] = useState(false)
  const [showLoose, setShowLoose] = useState(false)
  const [scrollTo, setScrollTo] = useState<string | null>(null)
  const list = useRef<HTMLDivElement>(null)
  useEffect(() => { void store.noticeUnplaced() }, [s.rows, s.boardLoaded])
  /* The Task Board page's request, spent once this conversation's board is
     here: the group opens (behind the Completed fold too), and the row is
     scrolled to after the render that draws it open. A group the board no
     longer has is dropped, never left to fire on a later redraw. */
  useEffect(() => {
    const key = sessionCurrent()
    const want = key && s.boardLoaded ? store.focusFor(key) : null
    if (!want) return
    store.clearFocus()
    const group = s.board?.groups.find((g) => g.id === want)
    if (!group) return
    if (group.status === 'completed') setShowDone(true)
    setOpenIds((prev) => new Set(prev).add(want))
    setScrollTo(want)
  }, [s.boardLoaded, s.board, s.focus])
  useEffect(() => {
    if (!scrollTo) return
    setScrollTo(null)
    const row = [...(list.current?.querySelectorAll<HTMLElement>('[data-group]') ?? [])].find((el) => el.dataset.group === scrollTo)
    row?.scrollIntoView({ block: 'nearest' })
  }, [scrollTo])

  const board = s.board
  if (!board || board.groups.length === 0) return flat(s.rows)

  const now = Date.now()
  const toggle = (id: string): void => setOpenIds((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })
  const loose = store.unplacedRows(s.rows, board, s.lineage)
  const done = store.completedGroups(board)
  return (
    <div className="tkboard" ref={list}>
      {loose.length > 0 && (
        <>
          <button type="button" className="tkunassigned" aria-expanded={showLoose} onClick={() => setShowLoose((v) => !v)}>
            <Icon icon={ArrowRight01Icon} size={13} stroke={2} />
            {t('gui.tasks.board.unassigned')} · {loose.length}
          </button>
          {showLoose && flat(loose)}
        </>
      )}
      <GroupRows
        groups={store.openGroups(board)} rows={s.rows} lineage={s.lineage} now={now}
        openIds={openIds} onToggle={toggle} done={false} openTask={openTask}
      />
      {done.length > 0 && (
        <>
          <button type="button" className="tkdonefold" aria-expanded={showDone} onClick={() => setShowDone((v) => !v)}>
            <Icon icon={ArrowRight01Icon} size={13} stroke={2} />
            {t('gui.tasks.board.completed', { n: done.length })}
          </button>
          {showDone && (
            <GroupRows
              groups={done} rows={s.rows} lineage={s.lineage} now={now}
              openIds={openIds} onToggle={toggle} done openTask={openTask}
            />
          )}
        </>
      )}
    </div>
  )
}
