/* The Agent Hub's rows and cards. The agents page lays its agents out as cards
 * in a grid; the onboarding wizard's agents step keeps the rows, a list under
 * section headings. Both answer the same three questions -- who it is (mark,
 * name, a line about what it is good at), how it is doing (a dot only where
 * there is something to say), and the one thing to do about it now. A row
 * opens the sheet only when its caller passes `onOpen`; a card always does.
 *
 * Connecting is the server's readiness ping -- one real prompt through the
 * agent, up to a minute -- so the row says "testing" for
 * its length and offers nothing else meanwhile; a refusal stays on the row as
 * red text with a Retry, rather than as a toast that is gone before the reader
 * looks up.
 */

import { AgentMark } from '../../components/AgentMark'
import { Glyph } from '../../components/Ico'
import { t } from '../../i18n/t'
import { ask as confirmAsk } from '../../state/confirm'
import { isOwnRow, shortOf } from './catalogue'
import { sectionOf, stageOf } from './source'
import * as store from './store'

import type { ExtAgentsState, Failure } from './store'
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

/* What a refused write comes to, in the reader's language: the fix the server
   named, by its kind, or what failed when it named none. The server's own
   sentence is English, and cut to the two lines a card has it said neither
   what went wrong nor what to do -- so it is shown only in the sheet, folded
   under the fix. */
function whatFailed(row: ExtAgentRow, failed: Failure): string {
  const agent = row.name
  const kind = failed.remedy?.kind
  if (kind === 'sign_in') return t('gui.agent.bad_sign_in', { agent })
  if (kind === 'setup') return t('gui.agent.bad_setup', { agent })
  if (kind === 'api_key') return t('gui.agent.bad_api_key', { agent })
  if (kind === 'download') return t('gui.agent.bad_download', { agent })
  const write = refusedWrite(failed)
  return t(write === 'save' ? 'gui.agent.bad_save' : write === 'disconnect' ? 'gui.agent.bad_disconnect' : 'gui.agent.bad_connect', {
    agent,
  })
}

/* Which write a refusal refused, which decides its words when no fix is named:
   a switch-off is not a connect (`disconnects` says so for the pending ring
   too), and an edit is neither. The sheet asks the same question, so a card and
   its sheet cannot say two things about one refusal. */
export function refusedWrite(failed: Failure): 'connect' | 'disconnect' | 'save' {
  if (store.disconnects(failed)) return 'disconnect'
  return failed.op === 'model' || failed.op === 'update' ? 'save' : 'connect'
}

/* The red line itself. A card or row that opens a sheet points there, where
   the steps and the original error are. The wizard's rows open nothing, so
   theirs has to carry the step: the command, when it is one to run in a
   terminal, and the retry to press after. */
function failureLine(row: ExtAgentRow, failed: Failure, opens: boolean): string {
  const what = whatFailed(row, failed)
  if (opens) return t('gui.agent.bad_open', { what })
  const button = t('gui.retry')
  const kind = failed.remedy?.kind
  /* The adapter's own command is too long for a row, and what fixes a
     download is the network anyway, so the row names where to look. */
  if (kind === 'download') return t('gui.agent.bad_download_retry', { what, button })
  const command = kind === 'sign_in' || kind === 'setup' ? failed.remedy?.command : ''
  return command ? t('gui.agent.bad_run', { what, command, button }) : t('gui.agent.bad_retry', { what, button })
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
    /* Raven's shipped specialists are part of Raven: never disconnected. */
    if (row.builtin || row.vendored) return null
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
          /* With no sheet to fold it into, the server's sentence -- all a row
             with no named fix has to say why -- is kept on hover, not shown. */
          <div className="extAgents-one extAgents-one-bad" title={open ? undefined : failed.detail}>
            {failureLine(row, failed, !!open)}
          </div>
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

const PLUS = 'M12 5v14M5 12h14'
const DOWNLOAD = 'M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19h14'
const RETRY = 'M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4h-4'

/* The card's corner control: Connect, Install, Retry, or the sign-in the
   agent wants. A connected card carries none -- Disconnect is in the sheet,
   one click away and out of reach of a stray click -- and a write in flight is
   said by the line itself. */
function CardControl({ row, shown }: { row: ExtAgentRow; shown: Shown }): JSX.Element | null {
  const iconButton = (label: string, d: string, onClick: () => void, cls = ''): JSX.Element => (
    <button
      aria-label={label}
      className={'extAgents-cbtn' + (cls ? ' ' + cls : '')}
      onClick={onClick}
      title={label}
      type="button"
    >
      <Glyph d={d} />
    </button>
  )
  if (shown === 'failed') return iconButton(t('gui.retry'), RETRY, () => store.retry(row), 'extAgents-cbtn-bad')
  if (shown === 'missing') return iconButton(t('gui.agent.go_install'), DOWNLOAD, () => store.sheetOpen(row))
  if (shown !== 'off') return null
  if (stageOf(row) === 'unauthorized') return <span className="extAgents-ctag">{t('gui.agent.unauthorized')}</span>
  return iconButton(t('gui.agent.connect'), PLUS, () => connect(row))
}

function AgentCard({ row, s }: { row: ExtAgentRow; s: ExtAgentsState }): JSX.Element {
  const shown = shownOf(row, s)
  const failed = s.failed[row.name]
  const open = (): void => store.sheetOpen(row)
  return (
    <div
      className="extAgents-card"
      role="button"
      tabIndex={0}
      aria-current={s.sheet === row.name ? 'true' : undefined}
      onClick={open}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          open()
        }
      }}
    >
      <div className="extAgents-ctop">
        <Tile row={row} />
        <div className="extAgents-nm">
          <span className="extAgents-t">{row.name}</span>
          <Led row={row} shown={shown} />
        </div>
        <div className="extAgents-ctl" onClick={(e) => e.stopPropagation()}>
          <CardControl row={row} shown={shown} />
        </div>
      </div>
      {shown === 'pending' ? (
        <div className="extAgents-one extAgents-one-work">
          <Spin />
          {t(pendingLabel(row, s))}
        </div>
      ) : shown === 'failed' && failed ? (
        <div className="extAgents-one extAgents-one-bad">{failureLine(row, failed, true)}</div>
      ) : (
        <div className="extAgents-one">{oneLine(row)}</div>
      )}
    </div>
  )
}

export function CardGrid({ rows, s, empty }: { rows: ExtAgentRow[]; s: ExtAgentsState; empty: string }): JSX.Element {
  if (!rows.length) return <div className="extAgents-empty">{empty}</div>
  return (
    <div className="extAgents-grid">
      {rows.map((row) => (
        <AgentCard key={row.name} row={row} s={s} />
      ))}
    </div>
  )
}

/* Raven's own first, then the server's order. */
export const ordered = (rows: ExtAgentRow[]): ExtAgentRow[] =>
  [...rows].sort((a, b) => Number(isOwnRow(b)) - Number(isOwnRow(a)))

export function SectionBlock({
  label, rows, s, onOpen, note, counted = true,
}: {
  label: string; rows: ExtAgentRow[]; s: ExtAgentsState; onOpen?: (row: ExtAgentRow) => void; note?: string
  /* Off for a heading that already says how many. */
  counted?: boolean
}): JSX.Element {
  return (
    <section className="extAgents-sec">
      <div className={counted ? 'extAgents-hd' : 'extAgents-hd extAgents-hd-say'}>
        <b>{label}</b>
        {counted ? <span className="extAgents-n">{String(rows.length)}</span> : null}
      </div>
      {note ? <div className="extAgents-empty">{note}</div> : null}
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
