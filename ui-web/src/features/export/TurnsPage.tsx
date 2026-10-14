/* The turn picker: one row per main-line turn, by its number, when it started
   and the reader's own words, with a select-all box over them. A turn still
   running cannot be chosen, and a turn the gateway refused says why. */

import { useEffect, useRef } from 'react'

import { t } from '../../i18n/t'
import * as store from './store'

import type { ExportState } from './store'
import type { JSX } from 'react'

const WHY: Record<string, string> = {
  in_progress: 'gui.export.why_in_progress',
  not_in_session: 'gui.export.why_not_in_session',
}

/** `YYYY-MM-DD HH:MM` in local time; the raw value when it does not parse. */
export function stamp(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const two = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`
}

export function TurnsPage({ s }: { s: ExportState }): JSX.Element {
  const all = useRef<HTMLInputElement>(null)
  const state = store.allState(s)

  useEffect(() => {
    if (all.current) all.current.indeterminate = state === 'some'
  }, [state])

  if (s.turns === null) {
    return s.error?.kind === 'load' ? (
      <div className="export-empty">
        <button type="button" className="export-btn export-ghost" onClick={store.retryTurns}>{t('gui.export.retry')}</button>
      </div>
    ) : <p className="export-empty">{t('gui.export.loading')}</p>
  }
  if (!s.turns.length) return <p className="export-empty">{t('gui.export.no_turns')}</p>

  return (
    <div className="export-turns">
      <p className="export-hint">{t('gui.export.turns_hint')}</p>
      <label className="export-head">
        <input
          ref={all}
          type="checkbox"
          className="export-all"
          checked={state === 'all'}
          onChange={store.toggleAll}
        />
        <span>{t('gui.export.select_all')}</span>
        <span className="export-grow" />
        <span className="export-count">{t('gui.export.selected_count', { n: s.selected.length })}</span>
      </label>
      <ul className="export-list">
        {s.turns.map((turn) => {
          const why = s.invalid[turn.trace_id]
          return (
            <li key={turn.trace_id}>
              <label className="export-turn" data-off={turn.in_progress || undefined} data-bad={why ? true : undefined}>
                <input
                  type="checkbox"
                  checked={s.selected.includes(turn.trace_id)}
                  disabled={turn.in_progress}
                  onChange={() => store.toggle(turn.trace_id)}
                />
                <span className="export-num">{t('gui.export.turn_number', { n: turn.number })}</span>
                <span className="export-when">{stamp(turn.start)}</span>
                <span className="export-preview">{turn.input_preview}</span>
                {turn.in_progress ? <span className="export-tag">{t('gui.export.in_progress')}</span> : null}
                {why ? <span className="export-why">{WHY[why] ? t(WHY[why]) : why}</span> : null}
              </label>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
