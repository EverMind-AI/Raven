import { useEffect, useRef, useState } from 'react'
import { useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { AgentMark } from '../../components/AgentMark'
import { KeyInput } from '../../components/KeyInput'
import { t } from '../../i18n/t'
import { ask as confirmAsk } from '../../state/confirm'
import * as lang from '../../state/lang'
import { byOf, installOf, isOwnRow, shortOf } from './catalogue'
import { sectionOf, stageOf } from './source'
import * as store from './store'

import type { Section } from './source'
import type { ExtAgentsState } from './store'
import type { ExtAgentRow } from './types'
import type { JSX } from 'react'
import './styles.css'

/* The agents this machine can hand work to, drawn to the Agent Hub prototype.
 *
 * Three sections answer the reader's three questions -- which agents work for
 * me now, which could I connect, which are not on this machine -- and a row
 * answers three more: who it is (mark, name, a line about what it is good at),
 * how it is doing (a dot that is only there when there is something to say),
 * and the one thing to do about it now. Everything else is in the sheet the
 * row opens: what the agent is good at, as the reader words it; a key, where
 * one is needed; how to install one that is absent; and the actions its state
 * calls for, in one bar.
 *
 * Connecting is the server's readiness ping -- up to a minute for a cli or acp
 * agent -- so the row and the sheet both say "connecting" for its length and
 * offer nothing else meanwhile; a refusal stays on the row as red text with a
 * Retry, rather than as a toast that is gone before the reader looks up.
 */

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
type Shown = 'pending' | 'failed' | 'missing' | 'on' | 'off'

function shownOf(row: ExtAgentRow, s: ExtAgentsState): Shown {
  if (s.joining.includes(row.name)) return 'pending'
  if (s.failed[row.name]) return 'failed'
  const section = sectionOf(row)
  return section === 'missing' ? 'missing' : section === 'on' ? 'on' : 'off'
}

function Spin(): JSX.Element {
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

function Tile({ row }: { row: ExtAgentRow }): JSX.Element {
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
function connect(row: ExtAgentRow): void {
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
function RowControl({ row, shown }: { row: ExtAgentRow; shown: Shown }): JSX.Element | null {
  if (shown === 'pending') return <span className="extAgents-state">{t('gui.agent.setup_connecting')}</span>
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
  return (
    <button className="mini go" onClick={() => connect(row)}>
      {t('gui.agent.connect')}
    </button>
  )
}

function AgentRow({ row, s }: { row: ExtAgentRow; s: ExtAgentsState }): JSX.Element {
  const shown = shownOf(row, s)
  const failed = s.failed[row.name]
  const open = (): void => store.sheetOpen(row)
  return (
    <div
      className="extAgents-row"
      role="button"
      tabIndex={0}
      aria-current={s.sheet === row.name ? 'true' : undefined}
      onClick={open}
      onKeyDown={(e) => {
        /* The row's own keys only: a keydown on the control inside bubbles to
           here, and preventing it would cancel that button's own activation. */
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          open()
        }
      }}
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
            {t('gui.agent.setup_connecting')}
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
        <RowControl row={row} shown={shown} />
      </div>
    </div>
  )
}

/* Raven's own first, then the server's order. */
const ordered = (rows: ExtAgentRow[]): ExtAgentRow[] =>
  [...rows].sort((a, b) => Number(isOwnRow(b)) - Number(isOwnRow(a)))

function SectionBlock({ label, rows, s }: { label: string; rows: ExtAgentRow[]; s: ExtAgentsState }): JSX.Element {
  return (
    <section className="extAgents-sec">
      <div className="extAgents-hd">
        <b>{label}</b>
        <span className="extAgents-n">{String(rows.length)}</span>
      </div>
      {rows.length ? (
        <div className="extAgents-set">
          {rows.map((row) => (
            <AgentRow key={row.name} row={row} s={s} />
          ))}
        </div>
      ) : null}
    </section>
  )
}

function OutIcon(): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M14 5h5v5" />
      <path d="M19 5l-7.5 7.5" />
      <path d="M17 14v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h4" />
    </svg>
  )
}

/* Whether `subagents.test` can answer for this row at all. A built-in agent is
   this process, and `run_test` refuses one outright. A discovered folder is
   looked up by neither name the call accepts, so both answer not-found;
   offering the button there would be offering a click that can only fail. A
   shipped product this install registered as a config row is found by
   `source: "config"` like any other. */
const canTest = (row: ExtAgentRow): boolean => !row.builtin && !row.vendored

/* What the agent is good at, in the reader's words. Committed when the field
   is left: Enter is a newline in a textarea, and a click away from a field one
   has just typed into means the typing. Left blank it goes back to what was
   there -- this is the text the dispatching model reads, and it cannot be
   nothing. */
function GoodAt({ row, saved, readOnly }: { row: ExtAgentRow; saved: string; readOnly: boolean }): JSX.Element {
  const [draft, setDraft] = useState(saved)
  useEffect(() => setDraft(saved), [saved])
  const commit = (): void => {
    const next = draft.trim()
    if (!next) {
      setDraft(saved)
      return
    }
    if (next !== saved) store.describe(row, next)
  }
  return (
    <label className="extAgents-fld">
      <span className="extAgents-k">{t('gui.agent.good_at')}</span>
      <textarea
        aria-label={t('gui.agent.good_at')}
        onBlur={commit}
        onChange={(e) => setDraft(e.target.value)}
        readOnly={readOnly}
        value={draft}
      />
    </label>
  )
}

/* The install block for an absent agent: the vendor's command with a copy
   button, and the vendor's site. Copy confirms itself on the button rather
   than in a toast, since the reader is looking at the button. */
function InstallBlock({ row }: { row: ExtAgentRow }): JSX.Element | null {
  const { site, cmd } = installOf(row)
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1400)
    return () => clearTimeout(timer)
  }, [copied])
  if (!site && !cmd) return null
  return (
    <div className="extAgents-inst">
      <span className="extAgents-k">{t('gui.plug.install')}</span>
      {cmd ? (
        <div className="extAgents-cmd">
          <code>{cmd}</code>
          <button
            type="button"
            onClick={() => {
              void navigator.clipboard?.writeText(cmd)
              setCopied(true)
            }}
          >
            {t(copied ? 'gui.agent.copied' : 'gui.agent.copy')}
          </button>
        </div>
      ) : null}
      {site ? (
        <a className="extAgents-site" href={`https://${site}`} rel="noreferrer" target="_blank">
          {site}
          <OutIcon />
        </a>
      ) : null}
    </div>
  )
}

/* One line under the name in the sheet: what is happening to this agent right
   now, or who makes it when nothing is. */
function StatusLine({ row, shown, s }: { row: ExtAgentRow; shown: Shown; s: ExtAgentsState }): JSX.Element {
  const by = byOf(row)
  const testing = s.testing.includes(row.name) || row.test_running
  if (shown === 'pending' || testing) {
    return (
      <div className="extAgents-by">
        <Spin />
        {t(testing ? 'gui.agent.testing_head' : 'gui.agent.setup_connecting')}
      </div>
    )
  }
  if (shown === 'failed') {
    return (
      <div className="extAgents-by extAgents-by-bad">
        <span className="extAgents-led extAgents-led-bad" />
        {s.failed[row.name]?.detail}
      </div>
    )
  }
  if (shown === 'on' && row.last_test_ok === false) {
    return (
      <div className="extAgents-by extAgents-by-bad">
        <span className="extAgents-led extAgents-led-bad" />
        {t('gui.agent.hd_test_bad', { detail: row.last_test_detail || '' })}
      </div>
    )
  }
  if (shown === 'on') {
    return (
      <div className="extAgents-by">
        <span className="extAgents-led" />
        {row.last_test_ok ? t('gui.agent.hd_on_tested') : t('gui.agent.hd_on_by', { by })}
      </div>
    )
  }
  if (shown === 'missing') return <div className="extAgents-by">{t('gui.agent.hd_missing_by', { by })}</div>
  return <div className="extAgents-by">{by}</div>
}

/* The sheet: identity and this moment's status, the fields the reader owns,
   and the actions the state calls for -- right-aligned, primary rightmost,
   the destructive one left of it. Every change lands as it is made, so there
   is no Save. The shared drawer's own close control floats at the top right;
   the head leaves it room. */
function AgentSheet({ row, s }: { row: ExtAgentRow; s: ExtAgentsState }): JSX.Element {
  const dHost = store.detailHost()
  const keyRef = useRef<HTMLInputElement>(null)
  const [keyTyped, setKeyTyped] = useState(false)
  const shown = shownOf(row, s)
  const stage = stageOf(row)
  const testing = s.testing.includes(row.name) || row.test_running
  const saveKey = (): void => {
    const api_key = keyRef.current ? keyRef.current.value.trim() : ''
    store.saveKey(row, api_key)
  }
  const needsKey = stage === 'key'
  const primaryDisabled = shown === 'pending' || (needsKey && !keyTyped)
  const primary = (): void => {
    if (needsKey) saveKey()
    else if (shown === 'failed') store.retry(row)
    else connect(row)
  }

  let actions: JSX.Element | null
  if (shown === 'missing') {
    actions = (
      <button className="mini go" disabled={s.loading} onClick={() => void store.recheck(row)}>
        {s.loading ? <Spin /> : null}
        {t(s.loading ? 'gui.agent.checking' : 'gui.agent.recheck')}
      </button>
    )
  } else if (shown === 'on') {
    actions = row.builtin ? null : (
      <>
        <button className="mini danger" onClick={() => store.disconnectRow(row)}>
          {t('gui.agent.disconnect')}
        </button>
        {canTest(row) ? (
          <button className="mini" disabled={testing} onClick={() => void store.runTest(row)}>
            {testing ? <Spin /> : null}
            {t(testing ? 'gui.agent.testing_chip' : 'gui.agent.test_label')}
          </button>
        ) : null}
      </>
    )
  } else {
    actions = (
      <button className="mini go" disabled={primaryDisabled} onClick={primary}>
        {shown === 'pending' ? <Spin /> : null}
        {t(shown === 'pending' ? 'gui.agent.setup_connecting' : shown === 'failed' ? 'gui.retry' : 'gui.agent.connect')}
      </button>
    )
  }

  return createPortal(
    <div className="extAgents-sheet" aria-label={row.name} role="document">
      <div className="extAgents-head">
        <Tile row={row} />
        <div className="extAgents-meta">
          <h3>{row.name}</h3>
          <StatusLine row={row} s={s} shown={shown} />
        </div>
      </div>
      <div className="extAgents-body">
        {shown === 'missing' ? (
          <>
            {row.description ? <div className="extAgents-about">{row.description}</div> : null}
            <InstallBlock row={row} />
            {s.stillMissing.includes(row.name) && !s.loading ? (
              <div className="extAgents-probe extAgents-probe-bad">{t('gui.agent.still_missing')}</div>
            ) : null}
          </>
        ) : (
          <>
            <GoodAt
              readOnly={!!row.builtin}
              row={row}
              saved={(!row.configured && !row.vendored && store.draftOf(row.name)) || row.description || ''}
            />
            {needsKey ? (
              <label className="extAgents-fld">
                <span className="extAgents-k">{t('gui.agent.key')}</span>
                <KeyInput
                  aria-label={t('gui.agent.key')}
                  onChange={(e) => setKeyTyped(!!e.target.value.trim())}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && keyTyped) saveKey()
                  }}
                  placeholder={row.has_api_key ? t('gui.agent.key_set') : ''}
                  ref={keyRef}
                />
              </label>
            ) : null}
          </>
        )}
      </div>
      <div className="extAgents-act">{actions}</div>
    </div>,
    dHost,
  )
}

export function ExtAgentsApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* The language the page resolved, so a pick repaints this island: every word
     below is a t(key) read at render time (state/lang/store.ts). */
  useSyncExternalStore(lang.subscribe, lang.get)
  const by = (section: Section): ExtAgentRow[] => ordered(s.rows.filter((row) => sectionOf(row) === section))
  const on = by('on')
  const avail = by('avail')
  const missing = by('missing')
  const sheetRow = s.sheet ? s.rows.find((x) => x.name === s.sheet) : undefined
  return (
    <>
      <div className="pmhero">
        <h3>{t('gui.page.agents')}</h3>
      </div>
      {/* The connected section is always there, even empty: it is the answer to
          the page's first question. The other two are only drawn with rows in
          them -- a heading over nothing is a heading about nothing. */}
      <SectionBlock label={t('gui.agent.g_on')} rows={on} s={s} />
      {avail.length ? <SectionBlock label={t('gui.agent.g_avail')} rows={avail} s={s} /> : null}
      {missing.length ? <SectionBlock label={t('gui.agent.g_missing')} rows={missing} s={s} /> : null}
      {sheetRow ? <AgentSheet key={`${s.sheet}:${s.epoch}`} row={sheetRow} s={s} /> : null}
    </>
  )
}
