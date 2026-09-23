import { useEffect, useRef, useState } from 'react'
import { useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { KeyInput } from '../../components/KeyInput'
import { ModelPicker } from '../../components/ModelPicker'
import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { defaultProviders as hostProviders, loadDefaultProviders } from '../model/source'
import { offered } from '../model/types'
import { byOf, installOf, isOwnRow } from './catalogue'
import { SectionBlock, Spin, Tile, connect, ordered, pendingLabel, shownOf } from './Rows'
import { sectionOf, stageOf } from './source'
import * as store from './store'

import type { PickerProvider } from '../../components/ModelPicker'
import type { Shown } from './Rows'
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
 * Connecting is the server's readiness ping -- one real prompt through the
 * agent, up to a minute -- so the row and the sheet both say "testing" for its
 * length and offer nothing else meanwhile; a refusal stays on the row as red text with a
 * Retry, rather than as a toast that is gone before the reader looks up.
 */

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
   this process, and `run_test` refuses one outright. Everything else has a
   command or an endpoint to dispatch once: a discovered folder is found by
   `source: "vendored"`, a shipped product this install registered as a config
   row by `source: "config"` like any other. */
const canTest = (row: ExtAgentRow): boolean => !row.builtin

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

/* The pill's menu, by the row's editing rule. The built-in row picks from
   raven's own connected providers, each with what it offers; an acp row picks
   from the choices its handshake advertised, bucketed the way the agent
   bucketed them. Empty is "no menu", whatever the rule. */
function pickerProvidersFor(row: ExtAgentRow): PickerProvider[] {
  if (row.model_source === 'raven') {
    /* The text models this host offers, by the same rule the composer's column
       reads: a provider with nothing added yet offers the registry's shortlist
       here too. Reading the added list alone drew a connected vendor with zero
       models beside a composer listing four. No pin -- the tick is the row's. */
    return hostProviders()
      .filter((p) => p.on)
      .map((p) => ({ id: p.id, name: p.name, models: offered(p, 'text'), labels: p.labels }))
  }
  if (row.model_source !== 'agent') return []
  const groups = new Map<string, PickerProvider>()
  for (const c of row.model_choices || []) {
    const id = c.group || row.name
    const group = groups.get(id) ?? { id, name: id, models: [], labels: {} }
    group.models.push(c.value)
    if (c.name) group.labels![c.value] = { label: c.name }
    groups.set(id, group)
  }
  return [...groups.values()]
}

/* The set model as the pill shows it: an acp choice by the name the agent gave
   it and its group; a host id split off the provider it is stored under, the
   way the host stores it. `provider` is what the picker's tick matches. */
function shownModel(row: ExtAgentRow): { id: string; by: string; provider: string } | null {
  if (!row.model) return null
  if (row.model_source === 'agent') {
    const hit = (row.model_choices || []).find((c) => c.value === row.model)
    if (hit) return { id: hit.name || row.model, by: hit.group || '', provider: hit.group || row.name }
    /* Not one of its choices. One of Raven's own can be carrying a host id it
       took while its menu was still empty -- it is measured behind the page,
       and the id stays what the row dispatches with -- so that one is drawn
       the way the host draws it rather than as an unattributed string. A third
       party's ids are its own, whatever they look like. */
    if (!isOwnRow(row) || !row.model.includes('/')) return { id: row.model, by: '', provider: row.name }
  }
  const cut = row.model.indexOf('/')
  const head = cut > 0 ? row.model.slice(0, cut) : ''
  /* The stored head is the provider's public spelling (`openai-codex`); the
     host list keys it by config slug (`openai_codex`). The two differ by the
     separator alone, so the lookup compares them in one spelling and the
     picker gets the slug back, which is what its tick matches. */
  const known = hostProviders().find((p) => slugOf(p.id) === slugOf(head))
  return { id: cut > 0 ? row.model.slice(cut + 1) : row.model, by: known ? known.name : head, provider: known ? known.id : head }
}

const slugOf = (name: string): string => name.toLowerCase().replace(/-/g, '_')

/* The row as the pill should draw it while a model write is in flight: what the
   write is asking for, rather than what the row still holds. The server proves a
   new model by running it now, so that write can take a minute, and a row
   reading "Testing" beside the model it is leaving reads as though the old one
   is the one under test.

   No rollback is needed and none is written: the pending entry goes when the
   write does, so a refusal puts the row's own value back on screen by itself.
   Handing the patched row to `shownModel` rather than reimplementing it keeps
   the choice lookup and the provider split in one place. */
function asAsked(row: ExtAgentRow, s: ExtAgentsState): ExtAgentRow {
  const write = s.joining[row.name]
  if (!write || write.op !== 'model') return row
  if (write.args.clear_model) return { ...row, model: null }
  return write.args.model ? { ...row, model: write.args.model } : row
}

/* The model a row answers with, and the picker that changes it. Unset reads by
   ownership: one of Raven's own follows the main Raven, a third party runs on
   its own default. A row with no menu wears the pill disabled: an openai or
   cli row (`fixed`) says "managed by itself" -- its configured model is not a
   pick and is not shown as one -- and an acp row whose handshake offered none
   says the same, unless it is Raven's own, which really does follow; a model
   such a row still carries is shown with its clear control, since the clear is
   the one write left. A server that predates the model field draws no pill:
   it has no model write. */
function ModelPill({ row, busy }: { row: ExtAgentRow; busy: boolean }): JSX.Element | null {
  const pill = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  if (!row.model_source) return null
  const own = isOwnRow(row)
  const provs = pickerProvidersFor(row)
  const fixed = row.model_source === 'fixed'
  /* The built-in row's list may simply not have landed yet; its click loads it. */
  const menuless = fixed || (row.model_source === 'agent' && !provs.length)
  const shown = fixed ? null : shownModel(row)
  /* `fixed` outranks ownership: one of raven's own whose folder carries its own
     chat credential runs on that key and the model beside it, so "follows the
     main Raven" was the one thing it does not do. */
  const unset = own && !fixed ? 'gui.agent.model_follow' : menuless ? 'gui.agent.model_managed' : 'gui.agent.model_own_default'
  const cls = [
    'extAgents-pill',
    menuless ? 'extAgents-pill-fixed' : '',
    shown ? '' : 'extAgents-pill-dim',
    shown ? 'extAgents-pill-clearable' : '',
  ]
    .filter(Boolean)
    .join(' ')
  const openPicker = async (): Promise<void> => {
    /* The host list is loaded at boot for the composer's chip; a sheet opened
       before that landed asks once itself rather than offering nothing. */
    if (row.model_source === 'raven' && !hostProviders().length) await loadDefaultProviders()
    setOpen(true)
  }
  return (
    <div className="extAgents-fld">
      <span className="extAgents-k">{t('gui.agent.model_label')}</span>
      <span className={cls}>
        <button
          aria-expanded={open}
          aria-label={t('gui.agent.model_change')}
          className="extAgents-pm"
          disabled={menuless || busy}
          onClick={() => void openPicker()}
          ref={pill}
          type="button"
        >
          {shown ? (
            <>
              <span className="extAgents-mid">{shown.id}</span>
              {shown.by ? <span className="extAgents-mpv">{shown.by}</span> : null}
            </>
          ) : (
            <span className="extAgents-mid">{t(unset)}</span>
          )}
          {menuless ? null : <span className="extAgents-mch">{'⌄'}</span>}
        </button>
        {shown ? (
          <button
            aria-label={t('gui.agent.model_clear')}
            className="extAgents-mx"
            disabled={busy}
            onClick={() => void store.clearModel(row)}
            type="button"
          >
            {'×'}
          </button>
        ) : null}
      </span>
      {open ? (
        <ModelPicker
          anchor={pill.current}
          current={shown ? { model: row.model_source === 'agent' ? row.model || '' : shown.id, provider: shown.provider } : null}
          emptyNote={t(row.model_source === 'raven' ? 'gui.agent.model_no_provider' : 'gui.agent.model_managed')}
          onClose={() => setOpen(false)}
          onPick={(model, provider) => {
            setOpen(false)
            void store.setModel(row, model, row.model_source === 'raven' ? provider : undefined)
          }}
          providers={provs}
          allowTyped={false}
          title={t('gui.agent.model_label')}
        />
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
        {t(testing ? 'gui.agent.testing_head' : pendingLabel(row, s))}
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
  /* Not only when connected: the sheet offers Test from the unauthorized state
     too, and a test pressed there can now fail on the agent's own answer while
     the handshake passes -- which clears the label and leaves nothing saying the
     test failed. `missing` keeps its own line, since "not found" outranks a
     verdict measured before the executable went away. */
  if (row.last_test_ok === false && shown !== 'missing') {
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
        {canTest(row) && testing ? (
          <button className="mini danger" onClick={() => store.stopTest(row)}>
            <Spin />
            {t('gui.stop')}
          </button>
        ) : canTest(row) ? (
          <button className="mini" onClick={() => void store.runTest(row)}>
            {t('gui.agent.test_label')}
          </button>
        ) : null}
      </>
    )
  } else if (stage === 'unauthorized' && shown === 'off') {
    /* The same sentence the row carries, and beside it the one press that
       can take it back: Test re-measures the handshake, and a sign-in that
       has happened since lets the row return to Connect. */
    actions = (
      <>
        <button className="mini" disabled>
          {t('gui.agent.unauthorized')}
        </button>
        {testing ? (
          <button className="mini danger" onClick={() => store.stopTest(row)}>
            <Spin />
            {t('gui.stop')}
          </button>
        ) : (
          <button className="mini" onClick={() => void store.runTest(row)}>
            {t('gui.agent.test_label')}
          </button>
        )}
      </>
    )
  } else {
    actions = (
      <button className="mini go" disabled={primaryDisabled} onClick={primary}>
        {shown === 'pending' ? <Spin /> : null}
        {t(shown === 'pending' ? pendingLabel(row, s) : shown === 'failed' ? 'gui.retry' : 'gui.agent.connect')}
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
            <ModelPill busy={shown === 'pending'} row={asAsked(row, s)} />
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
      <SectionBlock label={t('gui.agent.g_on')} onOpen={store.sheetOpen} rows={on} s={s} />
      {avail.length ? <SectionBlock label={t('gui.agent.g_avail')} onOpen={store.sheetOpen} rows={avail} s={s} /> : null}
      {missing.length ? <SectionBlock label={t('gui.agent.g_missing')} onOpen={store.sheetOpen} rows={missing} s={s} /> : null}
      {sheetRow ? <AgentSheet key={`${s.sheet}:${s.epoch}`} row={sheetRow} s={s} /> : null}
    </>
  )
}
