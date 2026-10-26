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

import { AgentMark, MIRASIM_GROUP } from '../../components/AgentMark'
import { Glyph } from '../../components/Ico'
import { t } from '../../i18n/t'
import { agentKindLabel } from '../../lib/agentKind'
import { ago } from '../../lib/ago'
import { ask as confirmAsk } from '../../state/confirm'
import { catalogueCards, familyOf, isOwnRow, shortOf } from './catalogue'
import { healthOf, kindOf, ledClass, pendingLabel, refusedWrite, shownOf } from './health'
import { stageOf } from './source'
import * as store from './store'

import type { Health, Shown } from './health'
import type { GridEntry } from './source'
import type { ExtAgentsState, Failure } from './store'
import type { ExtAgentRow, Remedy } from './types'
import type { JSX, ReactNode } from 'react'

export function Spin(): JSX.Element {
  return <span className="extAgents-spin" aria-hidden="true" />
}

/* The dot: `healthOf`'s verdict, or nothing. None for a row that is merely
   off or absent -- "not connected" is what the section already says -- and a
   failed test is drawn only on a connected row, by the same rule: an off row's
   section says it is not in use, and its sheet keeps the verdict. A test under
   way pulses on any row, since the sheet says the same of it. The verdict is
   the dot's accessible name; the tooltip sits on the name beside it, which is
   a target a pointer can find. */
function Led({ health, shown }: { health: Health; shown: Shown }): JSX.Element | null {
  if (health.tone === 'none' || (health.from === 'test' && shown !== 'on')) return null
  return <span aria-label={health.label} className={ledClass(health.tone)} role="img" />
}

const tooltip = (health: Health, shown: Shown): string | undefined =>
  health.tone === 'none' || (health.from === 'test' && shown !== 'on') ? undefined : health.label

const MACHINE = 'M4 5.5h16v9.5H4zM9.5 19h5M12 15.5V19'

/* Where the row runs, when not here: the registered machine's id in a chip
   after the name. The name is the user's -- `<preset>@<machine>` by default,
   but renamed at will -- so the chip is the one place the machine is always
   said. After the name on a row, which is wide; on a card, where the name
   row is a quarter of the page, it leads the line under the name instead.
   Nothing on a row that runs on this computer. */
function MachineTag({ row }: { row: ExtAgentRow }): JSX.Element | null {
  if (!row.machine) return null
  return (
    <span className="extAgents-mc" title={t('gui.agent.on_machine', { id: row.machine })}>
      <Glyph d={MACHINE} />
      {row.machine}
    </span>
  )
}

/* The plate a row wears. One of Mirasim's agents is that agent run through
   Mirasim, so its mark is the route's with the agent's over it -- and the pair
   is drawn from the two files the table already carries rather than a
   per-agent composite asset. `plain` is the picker's own row, where the agent
   is what the reader is choosing and the route is already the drawer's title. */
export function Tile({ row, plain }: { row: ExtAgentRow; plain?: boolean }): JSX.Element {
  const own = isOwnRow(row)
  const via = !plain && familyOf(row) ? { preset: MIRASIM_GROUP, badge: row.preset } : null
  return (
    <span className={'extAgents-tile' + (own ? ' extAgents-tile-own' : '')}>
      {via ? <AgentMark badge={via.badge} preset={via.preset} /> : <AgentMark preset={row.preset} own={own} />}
    </span>
  )
}

/* The line under the name: the catalogue's one sentence about the agent, or --
   for a row nobody catalogued -- the probe's own verdict when it has one, else
   how Raven reaches it. A refusal replaces it in red; a write in flight
   replaces it with the ring. */
export function oneLine(row: ExtAgentRow): string {
  const short = shortOf(row)
  const stale = stageOf(row) === 'stale' ? t('gui.agent.tag_stale') : ''
  const base =
    short || ((row.probe_status === 'attention' || row.probe_status === 'missing') && row.probe_detail) || agentKindLabel(row.kind)
  return stale ? `${base} · ${stale}` : base
}

/* The one word for what went wrong, by the write that was refused. */
export function refusedLabel(failed: Failure): string {
  const write = refusedWrite(failed)
  return t(write === 'save' ? 'gui.agent.st_save_bad' : write === 'disconnect' ? 'gui.agent.st_disconnect_bad' : 'gui.agent.st_connect_bad')
}

/* The short reason, by the fix the server named. Spelled out rather than built
   from the kind, so the i18n gate can see every key. */
const WHY: Record<Remedy['kind'], string> = {
  sign_in: 'gui.agent.why_sign_in',
  setup: 'gui.agent.why_setup',
  api_key: 'gui.agent.why_api_key',
  download: 'gui.agent.why_download',
  model: 'gui.agent.why_model',
  billing: 'gui.agent.why_billing',
  quota: 'gui.agent.why_quota',
  network: 'gui.agent.why_network',
  silent: 'gui.agent.why_silent',
  upgrade: 'gui.agent.why_upgrade',
  exited: 'gui.agent.why_exited',
  runtime: 'gui.agent.why_runtime',
  plan: 'gui.agent.why_plan',
  config: 'gui.agent.why_config',
}

export function whyOf(remedy: Remedy): string {
  return t(WHY[kindOf(remedy)])
}

/* What a card or row says about the last thing that went wrong, under the
   line about the agent rather than instead of it: one word for what failed, a
   short reason, and the press that answers it (the card's corner, a row's
   button). Two sources, in this order: a
   write this page refused (held on the page, gone with the next write on the
   row), then a test the server remembered failing (on the row, so it survives
   a reload). A test under way shows as such, so the press cannot be pressed
   twice. Nothing for a row whose state already says it all: pending, absent,
   or waiting on a sign-in. */
export interface Verdict {
  label: string
  /* The short reason, or '' when the label is the whole of it. */
  why: string
  /* The server's own sentence, for a row with no sheet to fold it into. */
  raw: string
  /* The fix the server named, when it did. */
  remedy: Remedy | null
  act: string
  onAct: () => void
  busy?: boolean
  /* Nothing wrong: the line says how the row is doing, with no wash. */
  quiet?: boolean
}

/* What a card says when there is nothing to answer: which state it is in,
   in the words the tabs use, so the slot under the line is never blank and a
   grid reads the same whether or not a card is in trouble. A passed test says
   when it passed. Not the vendor: the mark beside the name already says that. */
export function quietOf(row: ExtAgentRow, shown: Shown, health: Health): Verdict {
  let label: string
  if (health.tone === 'warn') {
    label = health.label
  } else if (shown === 'on') {
    label = row.last_test_ok ? [t('gui.agent.hd_on_tested'), ago(row.last_test_at_ms)].filter(Boolean).join(' · ') : t('gui.agent.g_on')
  } else if (shown === 'missing') {
    label = t('gui.agent.g_missing')
  } else {
    label = t('gui.agent.g_avail')
  }
  return { label, why: '', raw: '', remedy: null, act: '', onAct: () => {}, quiet: true }
}

function busyVerdict(label: string): Verdict {
  return { label, why: '', raw: '', remedy: null, act: '', onAct: () => {}, busy: true }
}

export function verdictOf(row: ExtAgentRow, s: ExtAgentsState, shown: Shown, opens: boolean): Verdict | null {
  if (shown === 'pending' || shown === 'missing') return null
  const failed = s.failed[row.name]
  if (failed) {
    return {
      label: refusedLabel(failed),
      why: failed.remedy ? whyOf(failed.remedy) : t(opens ? 'gui.agent.why_open' : 'gui.agent.why_hover'),
      raw: failed.detail,
      remedy: failed.remedy || null,
      act: t('gui.retry'),
      onAct: () => store.retry(row),
    }
  }
  if (stageOf(row) === 'unauthorized') return null
  if (s.testing.includes(row.name) || row.test_running) return busyVerdict(t('gui.agent.testing'))
  if (row.last_test_ok === false) {
    return {
      label: t('gui.agent.st_test_bad'),
      why: row.last_test_remedy ? whyOf(row.last_test_remedy) : ago(row.last_test_at_ms),
      raw: row.last_test_detail,
      remedy: row.last_test_remedy || null,
      act: t('gui.agent.test_again_short'),
      onAct: () => void store.runTest(row),
    }
  }
  return null
}

/* The verdict as a card's footer: a strip under the line about the agent.
   It only says; the press that answers it is the card's corner control. */
function Foot({ v }: { v: Verdict }): JSX.Element {
  return (
    <div className={'extAgents-foot' + (v.busy ? ' extAgents-foot-busy' : '') + (v.quiet ? ' extAgents-foot-quiet' : '')}>
      <span className="extAgents-foot-say">
        <span className="extAgents-foot-k">
          {v.busy ? <Spin /> : null}
          {v.label}
        </span>
        {v.why ? <span className="extAgents-foot-r">{v.why}</span> : null}
      </span>
    </div>
  )
}

/* Private-use characters: never in a catalogue string, and left alone by the
   JSON the test double renders its arguments with. */
const MARK = '\uE000'
const MARK_THEN = '\uE001'

/* A sentence with commands in it, as text with each command a piece of code:
   split around markers the catalogue cannot contain. */
function withCode(sentence: string, codes: Record<string, string>): ReactNode {
  return sentence.split(/(\uE000|\uE001)/).map((part, i) =>
    part in codes ? <code key={i}>{codes[part]}</code> : part,
  )
}

/* The verdict as a row's second line. A row with no sheet has to carry the
   step itself: the command to run as a piece of code -- and what to type once
   it runs, when the fix is inside the agent; a command that only makes the
   agent say why it fails is named as that -- or where to look for a download;
   and the server's sentence on hover, when there is no fix. */
function RowVerdict({ v, opens }: { v: Verdict; opens: boolean }): JSX.Element {
  const kind = v.remedy?.kind
  const command = !opens && kind && kind !== 'api_key' && kind !== 'download' ? v.remedy?.command || '' : ''
  const then = command ? v.remedy?.then || '' : ''
  let why: ReactNode = v.why
  if (command) {
    const vars = then ? { why: v.why, command: MARK, then: MARK_THEN, button: v.act } : { why: v.why, command: MARK, button: v.act }
    /* A plan's `command` is the page that sells one, opened in a browser; a
       config's and a network's only make the agent say what is wrong. */
    const key =
      kind === 'plan'
        ? 'gui.agent.row_open'
        : then
          ? 'gui.agent.row_run_then'
          : kind === 'network' || kind === 'silent' || kind === 'config'
            ? 'gui.agent.row_run_diagnose'
            : 'gui.agent.row_run'
    why = withCode(t(key, vars), { [MARK]: command, [MARK_THEN]: then })
  } else if (!opens && kind === 'download') {
    why = t('gui.agent.row_download', { button: v.act })
  }
  return (
    <div className={'extAgents-row2' + (v.busy ? ' extAgents-foot-busy' : '')} title={opens || v.remedy || !v.raw ? undefined : v.raw}>
      <span className="extAgents-foot-k">
        {v.busy ? <Spin /> : null}
        {v.label}
      </span>
      {v.why ? <span className="extAgents-foot-r">{why}</span> : null}
    </div>
  )
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
      t('gui.agent.migrate_body', { name: row.name, to: agentKindLabel(row.upgrade_to) }),
      t('gui.agent.migrate_do'),
      () => store.connectRow(row),
    )
    return
  }
  void store.connectRow(row)
}

/* The one control a row carries. Exactly one, or none for the built-in loop,
   which is always on and has nothing to do. */
function RowControl({ row, s, shown }: { row: ExtAgentRow; s: ExtAgentsState; shown: Shown }): JSX.Element | null {
  if (shown === 'pending') return <span className="extAgents-state">{t(pendingLabel(row, s))}</span>
  if (shown === 'failed') {
    return (
      <button className="mini" onClick={() => store.retry(row)}>
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
    /* The built-in loop alone has no switch: an unnamed spawn and a dag node
       with no sub-agent dispatch to it. Raven's shipped specialists switch off
       like any other agent; coming with Raven does not keep one on the roster. */
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
  const health = healthOf(row, s)
  const open = onOpen ? (): void => onOpen(row) : undefined
  const verdict = verdictOf(row, s, shown, !!open)
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
        <div className="extAgents-nm" title={tooltip(health, shown)}>
          <Led health={health} shown={shown} />
          <span className="extAgents-t">{row.name}</span>
          <MachineTag row={row} />
        </div>
        {shown === 'pending' ? (
          <div className="extAgents-one extAgents-one-work">
            <Spin />
            {t(pendingLabel(row, s))}
          </div>
        ) : verdict ? (
          <RowVerdict opens={!!open} v={verdict} />
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

const RETRY = 'M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4h-4'
const PLUS = 'M12 5v14M5 12h14'
const DOWNLOAD = 'M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19h14'

/* The corner holds the one press the card calls for, so every press on a
   card is in the same place: connect, install, and after a refusal the retry;
   on a connected agent whose last test failed, the retest. A card still to
   connect keeps its connect over a retest, since connecting tests it anyway. */
function CardControl({ row, shown, verdict }: { row: ExtAgentRow; shown: Shown; verdict: Verdict | null }): JSX.Element | null {
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
  if (shown === 'failed' && verdict) return iconButton(verdict.act, RETRY, verdict.onAct)
  if (shown === 'missing') return iconButton(t('gui.agent.go_install'), DOWNLOAD, () => store.sheetOpen(row))
  if (shown === 'on' && verdict && !verdict.busy && !verdict.quiet) return iconButton(verdict.act, RETRY, verdict.onAct)
  if (shown !== 'off') return null
  if (stageOf(row) === 'unauthorized') return <span className="extAgents-ctag">{t('gui.agent.unauthorized')}</span>
  return iconButton(t('gui.agent.connect'), PLUS, () => connect(row))
}

/* A card's own write in flight wears the same amber strip as a test under
   way, so the line about the agent stays put. The slot under the line is
   always filled -- a quiet word on how the row is doing when nothing is
   wrong -- so a card is the same height before a press, during it and after a
   refusal. */
function AgentCard({ row, s }: { row: ExtAgentRow; s: ExtAgentsState }): JSX.Element {
  const shown = shownOf(row, s)
  const health = healthOf(row, s)
  const verdict = shown === 'pending' ? busyVerdict(t(pendingLabel(row, s))) : verdictOf(row, s, shown, true) || quietOf(row, shown, health)
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
        <div className="extAgents-nm" title={tooltip(health, shown)}>
          <span className="extAgents-t">{row.name}</span>
          <Led health={health} shown={shown} />
        </div>
        <div className="extAgents-ctl" onClick={(e) => e.stopPropagation()}>
          <CardControl row={row} shown={shown} verdict={verdict} />
        </div>
      </div>
      <div className="extAgents-one">
        <MachineTag row={row} />
        {oneLine(row)}
      </div>
      <div className="extAgents-foot-slot">
        <Foot v={verdict} />
      </div>
    </div>
  )
}

/* The grid before the first answer: one card of the fixed shape a real card
   has per catalogue entry, which is the roster on every machine, so it lands
   in place instead of after an empty message and a jump. Bars, not rows: a
   card would be claiming a state the server has not confirmed. Cards, not
   keys: a family is one cell in the answer and so one cell here, or the grid
   would jump by eight the moment the roster landed. */
export function WaitGrid(): JSX.Element {
  return (
    <div className="extAgents-grid" aria-busy="true">
      {Array.from({ length: catalogueCards() }, (_, i) => (
        <div className="extAgents-card extAgents-wcard" key={i}>
          <div className="extAgents-ctop">
            <span className="extAgents-wbar extAgents-wtile" />
            <span className="extAgents-wbar extAgents-wname" />
          </div>
          <span className="extAgents-wbar extAgents-wline" />
          <div className="extAgents-foot-slot">
            <span className="extAgents-wbar extAgents-wfoot" />
          </div>
        </div>
      ))}
    </div>
  )
}

/* The card that stands in for a family of presets the reader has not opened.
   Its job is one press: show the agents behind it. It reads like every other
   card -- mark, name, a line, a filled foot -- because it IS one of the grid's
   cells and a different shape would read as a different kind of thing. */
function FamilyCard({ entry, s }: { entry: Extract<GridEntry, { kind: 'family' }>; s: ExtAgentsState }): JSX.Element {
  const open = (): void => store.pickerOpen(entry.family)
  return (
    <div
      className="extAgents-card"
      role="button"
      tabIndex={0}
      aria-current={s.picking === entry.family ? 'true' : undefined}
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
        <span className="extAgents-tile">
          <AgentMark preset={MIRASIM_GROUP} />
        </span>
        <div className="extAgents-nm">
          <span className="extAgents-t">{t('gui.agent.fam_mirasim')}</span>
        </div>
        <div className="extAgents-ctl" onClick={(e) => e.stopPropagation()}>
          <span className="extAgents-ctag">{t('gui.agent.fam_n', { n: entry.members.length })}</span>
        </div>
      </div>
      <div className="extAgents-one">{t('gui.agent.fam_mirasim_about')}</div>
      <div className="extAgents-foot-slot">
        <Foot v={{ label: t('gui.agent.fam_pick'), why: '', raw: '', remedy: null, act: '', onAct: () => {}, quiet: true }} />
      </div>
    </div>
  )
}

export function CardGrid({
  entries, s, empty,
}: { entries: GridEntry[]; s: ExtAgentsState; empty: string }): JSX.Element {
  if (!entries.length) return <div className="extAgents-empty">{empty}</div>
  return (
    <div className="extAgents-grid">
      {entries.map((entry) =>
        entry.kind === 'family' ? (
          /* Family AND section: see GridEntry -- one key per population, or
             React keeps the first of two cards and silently drops the other. */
          <FamilyCard entry={entry} key={`${entry.family}:${entry.section}`} s={s} />
        ) : (
          <AgentCard key={entry.row.name} row={entry.row} s={s} />
        ),
      )}
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
