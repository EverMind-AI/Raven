/* The Agent Hub's rows, shared by the agents page and the onboarding wizard's
 * agents step. A row answers three questions -- who it is (mark, name, a line
 * about what it is good at), how it is doing (a dot only where there is
 * something to say), and the one thing to do about it now -- and a section is
 * a heading, a count and its rows. What differs between the two callers is
 * only whether a row opens the sheet, which is the caller's `onOpen`.
 *
 * Connecting is the server's readiness ping -- one real prompt through the
 * agent, up to a minute -- so the row says "testing" for
 * its length and offers nothing else meanwhile; a refusal stays on the row as
 * red text with a Retry, rather than as a toast that is gone before the reader
 * looks up.
 */

import { AgentMark } from '../../components/AgentMark'
import { t } from '../../i18n/t'
import { ask as confirmAsk } from '../../state/confirm'
import { isOwnRow, shortOf } from './catalogue'
import { sectionOf, stageOf } from './source'
import * as store from './store'

import type { ExtAgentsState } from './store'
import type { ExtAgentRow } from './types'
import type { JSX } from 'react'

const kindText = (kind: string): string =>
  t(
    kind === 'builtin'
      ? 'gui.agent.kind_builtin'
      : kind === 'openai'
        ? 'gui.agent.kind_openai'
        : kind === 'acp'
          ? 'gui.agent.kind_acp'
          : 'gui.agent.kind_cli',
  )

/* The states a row and its sheet are drawn in. `pending` and `failed` are
   this page's own, about a write in flight or refused; the other three are the
   section the server's facts put the row in. */
export type Shown = 'pending' | 'failed' | 'missing' | 'on' | 'off'

/* The word a row wears while its own write is in flight. One `pending` covers
   every write this page makes, so the word comes from the write rather than
   from the state: a switch-on waits on the readiness ping and says it is
   testing, a switch-off waits on its own write alone and says it is
   disconnecting, and the rest -- a key, a model, a description -- keep the
   older word, being neither. */
export function pendingLabel(row: ExtAgentRow, s: ExtAgentsState): string {
  const write = s.joining[row.name]
  if (!write) return 'gui.agent.setup_connecting'
  if (store.probes(write)) return 'gui.agent.testing'
  return store.disconnects(write) ? 'gui.agent.disconnecting' : 'gui.agent.setup_connecting'
}

export function shownOf(row: ExtAgentRow, s: ExtAgentsState): Shown {
  if (row.name in s.joining) return 'pending'
  if (s.failed[row.name]) return 'failed'
  const section = sectionOf(row)
  return section === 'missing' ? 'missing' : section === 'on' ? 'on' : 'off'
}

export function Spin(): JSX.Element {
  return <span className="extAgents-spin" aria-hidden="true" />
}

/* The dot: green for a working agent, gold when the probe has a caveat, amber
   and pulsing while a write is in flight, red for a refusal. None at all for a
   row that is merely off or absent -- "not connected" is what the section
   already says. */
function Led({ row, shown }: { row: ExtAgentRow; shown: Shown }): JSX.Element | null {
  if (shown === 'pending') return <span className="extAgents-led extAgents-led-busy" />
  if (shown === 'failed') return <span className="extAgents-led extAgents-led-bad" />
  if (shown !== 'on') return null
  const warn = !row.builtin && row.probe_status === 'attention'
  return <span className={'extAgents-led' + (warn ? ' extAgents-led-warn' : '')} />
}

export function Tile({ row }: { row: ExtAgentRow }): JSX.Element {
  const own = isOwnRow(row)
  return (
    <span className={'extAgents-tile' + (own ? ' extAgents-tile-own' : '')}>
      <AgentMark preset={row.preset} own={own} />
    </span>
  )
}

/* The line under the name: the catalogue's one sentence about the agent, or --
   for a row nobody catalogued -- the probe's own verdict when it has one, else
   how Raven reaches it. A refusal replaces it in red; a write in flight
   replaces it with the ring. */
function oneLine(row: ExtAgentRow): string {
  const short = shortOf(row)
  const stale = stageOf(row) === 'stale' ? t('gui.agent.tag_stale') : ''
  const base =
    short || ((row.probe_status === 'attention' || row.probe_status === 'missing') && row.probe_detail) || kindText(row.kind)
  return stale ? `${base} · ${stale}` : base
}

/* Connect, by what the row's stage calls for. The one case with a question in
   it is a preset that moved transport: connecting it removes the entry and adds
   it back from the preset, which drops the handles of runs already in flight. */
export function connect(row: ExtAgentRow): void {
  if (stageOf(row) === 'key') {
    store.sheetOpen(row)
    return
  }
  if (stageOf(row) === 'stale') {
    confirmAsk(
      t('gui.agent.migrate_do'),
      t('gui.agent.migrate_body', { name: row.name, to: kindText(row.upgrade_to || '') }),
      t('gui.agent.migrate_do'),
      () => store.connectRow(row),
    )
    return
  }
  store.connectRow(row)
}

/* The one control a row carries. Exactly one, or none for the built-in loop,
   which is always on and has nothing to do. */
function RowControl({ row, s, shown }: { row: ExtAgentRow; s: ExtAgentsState; shown: Shown }): JSX.Element | null {
  if (shown === 'pending') return <span className="extAgents-state">{t(pendingLabel(row, s))}</span>
  if (shown === 'failed') {
    return (
      <button className="mini danger" onClick={() => store.retry(row)}>
        {t('gui.retry')}
      </button>
    )
  }
  if (shown === 'missing') {
    return (
      <button className="mini" onClick={() => store.sheetOpen(row)}>
        {t('gui.agent.go_install')}
      </button>
    )
  }
  if (shown === 'on') {
    if (row.builtin) return null
    return (
      <button className="mini" onClick={() => store.disconnectRow(row)}>
        {t('gui.agent.disconnect')}
      </button>
    )
  }
  if (stageOf(row) === 'unauthorized') {
    return (
      <button className="mini" disabled>
        {t('gui.agent.unauthorized')}
      </button>
    )
  }
  return (
    <button className="mini go" onClick={() => connect(row)}>
      {t('gui.agent.connect')}
    </button>
  )
}

/* `onOpen` is what a click on the row does -- the hub opens the sheet. Without
   one the row is a plain row: no button role, no focus stop, nothing to press.
   The onboarding wizard draws it that way, since a wizard step is a decision
   and has no sheet to open. */
function AgentRow({
  row, s, onOpen,
}: { row: ExtAgentRow; s: ExtAgentsState; onOpen?: (row: ExtAgentRow) => void }): JSX.Element {
  const shown = shownOf(row, s)
  const failed = s.failed[row.name]
  const open = onOpen ? (): void => onOpen(row) : undefined
  return (
    <div
      className="extAgents-row"
      role={open ? 'button' : undefined}
      tabIndex={open ? 0 : undefined}
      aria-current={open && s.sheet === row.name ? 'true' : undefined}
      onClick={open}
      onKeyDown={open ? (e) => {
        /* The row's own keys only: a keydown on the control inside bubbles to
           here, and preventing it would cancel that button's own activation. */
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          open()
        }
      } : undefined}
    >
      <Tile row={row} />
      <div className="extAgents-who">
        <div className="extAgents-nm">
          <Led row={row} shown={shown} />
          <span className="extAgents-t">{row.name}</span>
        </div>
        {shown === 'pending' ? (
          <div className="extAgents-one extAgents-one-work">
            <Spin />
            {t(pendingLabel(row, s))}
          </div>
        ) : shown === 'failed' && failed ? (
          <div className="extAgents-one extAgents-one-bad">{failed.detail}</div>
        ) : (
          <div className="extAgents-one">{oneLine(row)}</div>
        )}
      </div>
      {/* The control stops the click here: pressing Connect must not also open
          the sheet. */}
      <div className="extAgents-ctl" onClick={(e) => e.stopPropagation()}>
        <RowControl row={row} s={s} shown={shown} />
      </div>
    </div>
  )
}

/* Raven's own first, then the server's order. */
export const ordered = (rows: ExtAgentRow[]): ExtAgentRow[] =>
  [...rows].sort((a, b) => Number(isOwnRow(b)) - Number(isOwnRow(a)))

export function SectionBlock({
  label, rows, s, onOpen,
}: { label: string; rows: ExtAgentRow[]; s: ExtAgentsState; onOpen?: (row: ExtAgentRow) => void }): JSX.Element {
  return (
    <section className="extAgents-sec">
      <div className="extAgents-hd">
        <b>{label}</b>
        <span className="extAgents-n">{String(rows.length)}</span>
      </div>
      {rows.length ? (
        <div className="extAgents-set">
          {rows.map((row) => (
            <AgentRow key={row.name} onOpen={onOpen} row={row} s={s} />
          ))}
        </div>
      ) : null}
    </section>
  )
}
