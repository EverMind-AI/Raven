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

import { useEffect, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import { ago } from '../../lib/ago'
import { current as sessionCurrent } from '../../lib/session'
import { revealTurnAt } from '../transcript/source'
import * as store from './store'

import type { LinkedRow } from './store'
import type { TaskGroup, TaskRow } from './types'
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

function Chip({ linked, openTask }: { linked: LinkedRow; openTask: OpenTask }): JSX.Element {
  const { link, row } = linked
  const label = `${link.kind} · ${row?.task_summary || link.summary || link.id}`
  /* A Task `tasks.list` no longer holds -- a branched conversation, a removed
     run -- is still named, but there is no pane to open. */
  if (!row) return <span className="tkchip" data-gone="1" title={label}>{label}</span>
  return (
    <button type="button" className="tkchip" data-st={row.status} onClick={() => openTask(row)}>
      {label}
    </button>
  )
}

async function toggleDone(group: TaskGroup): Promise<void> {
  const src = store.source()
  const key = sessionCurrent()
  if (!src?.setGroupStatus || !key) return
  const got = await src.setGroupStatus(key, group.id, group.status === 'completed' ? 'active' : 'completed')
  store.onBoardUpdated({ session_key: key, board: got.board, lineage: got.lineage })
}

function Thread({ group, rows, lineage, now, openTask }: {
  group: TaskGroup; rows: TaskRow[]; lineage: Record<string, string>; now: number; openTask: OpenTask
}): JSX.Element {
  return (
    <div className="tkthread" id={`tkthread-${group.id}`}>
      <ul className="tksteps">
        {group.steps.map((step) => (
          <li key={step.id} className="tkstep">
            <button type="button" className="tksteptext" onClick={() => revealTurnAt(step.turn_at)}>{step.text}</button>
            <div className="tkstepmeta">
              <time>{stepTime(step.turn_at, now)}</time>
              {store.linkedRows(step, rows, lineage).map((l) => <Chip key={`${l.link.kind}:${l.link.id}`} linked={l} openTask={openTask} />)}
            </div>
          </li>
        ))}
      </ul>
      <button type="button" className="tkgroupact" onClick={() => void toggleDone(group)}>
        {group.status === 'completed' ? t('gui.tasks.board.reopen') : t('gui.tasks.board.mark_completed')}
      </button>
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
        const last = group.steps[group.steps.length - 1]
        return [
          <button
            key={group.id}
            type="button"
            className={'sarow tkgroup' + (done ? ' tkdone' : '')}
            data-group={group.id}
            data-st={group.status}
            aria-expanded={open}
            aria-controls={`tkthread-${group.id}`}
            title={t(`gui.tasks.board.st_${group.status}`)}
            onClick={() => onToggle(group.id)}
          >
            <span className="tkdot" data-st={group.status} />
            <div className="bd">
              <div className="tline">
                <span className="nm">{group.title}</span>
                <span className="tkgmeta">{metaOf(group, rows, lineage, now)}</span>
              </div>
              <s className="st">{last?.text ?? ''}</s>
            </div>
          </button>,
          open ? (
            <Thread key={`${group.id}:thread`} group={group} rows={rows} lineage={lineage} now={now} openTask={openTask} />
          ) : null,
        ]
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
  useEffect(() => { void store.noticeUnplaced() }, [s.rows, s.boardLoaded])

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
    <div className="salist tasks">
      {loose.length > 0 && (
        <>
          <button type="button" className="tkunassigned" aria-expanded={showLoose} onClick={() => setShowLoose((v) => !v)}>
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
