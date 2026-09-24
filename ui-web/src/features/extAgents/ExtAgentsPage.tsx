import { useEffect, useRef, useState } from 'react'
import { useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { KeyInput } from '../../components/KeyInput'
import { ModelPicker } from '../../components/ModelPicker'
import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { defaultProviders as hostProviders, loadDefaultProviders } from '../model/source'
import { offered, withCurrent } from '../model/types'
import { byOf, installOf, isOwnRow } from './catalogue'
import { badOf, healthOf, kindOf, ledClass, pendingLabel, refusedWrite, shownOf } from './health'
import { CardGrid, Spin, Tile, WaitGrid, ago, connect, ordered, refusedLabel } from './Rows'
import { sectionOf, stageOf } from './source'
import * as store from './store'

import type { PickerProvider } from '../../components/ModelPicker'
import type { Shown } from './health'
import type { Section } from './source'
import type { ExtAgentsState } from './store'
import type { ExtAgentRow, Remedy } from './types'
import type { JSX } from 'react'
import './styles.css'

/* The agents this machine can hand work to, drawn to the Agent Hub prototype.
 *
 * One grid of cards, filtered by tabs that answer the reader's three questions
 * -- which agents work for me now, which could I connect, which are not on
 * this machine -- and a card answers three more: who it is (mark, name, a line
 * about what it is good at), how it is doing (a dot that is only there when
 * there is something to say), and the one thing to do about it now. Everything
 * else is in the sheet the card opens: what the agent is good at, as the
 * reader words it; a key, where
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
   nothing. The built-in loop's line is not the reader's to word, so it is
   drawn as text rather than as a field that will not take typing. */
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
  if (readOnly) {
    return (
      <div className="extAgents-fld">
        <span className="extAgents-k">{t('gui.agent.good_at')}</span>
        <p className="extAgents-ro">{saved}</p>
      </div>
    )
  }
  return (
    <label className="extAgents-fld">
      <span className="extAgents-k">{t('gui.agent.good_at')}</span>
      <textarea
        aria-label={t('gui.agent.good_at')}
        onBlur={commit}
        onChange={(e) => setDraft(e.target.value)}
        value={draft}
      />
    </label>
  )
}

/* A command the reader runs in a terminal, with a copy button. Copy confirms
   itself on the button rather than in a toast, since the reader is looking at
   the button. Shared by the install block and a refusal's fix, which is the
   same act: a command to run somewhere this page cannot reach. */
function CmdCopy({ cmd }: { cmd: string }): JSX.Element {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1400)
    return () => clearTimeout(timer)
  }, [copied])
  return (
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
  )
}

/* The install block for an absent agent: the vendor's command with a copy
   button, and the vendor's site. */
function InstallBlock({ row }: { row: ExtAgentRow }): JSX.Element | null {
  const { site, cmd, node } = installOf(row)
  if (!site && !cmd) return null
  return (
    <div className="extAgents-inst">
      {node ? (
        <div className="extAgents-about">
          {t('gui.agent.needs_node', { agent: row.name, button: t('gui.agent.recheck') })}
        </div>
      ) : null}
      <span className="extAgents-k">{t('gui.plug.install')}</span>
      {cmd ? <CmdCopy cmd={cmd} /> : null}
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
    /* Plus whatever this row already holds, where its own provider's column
       does not carry it: a model added by hand, or one the provider has since
       stopped listing, would otherwise open a picker with nothing marked. */
    const held = shownModel(row)
    return hostProviders()
      .filter((p) => p.on)
      .map((p) => ({
        id: p.id,
        name: p.name,
        models: withCurrent(p, offered(p, 'text'), held && held.provider === p.id ? held.id : null),
        labels: p.labels,
      }))
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
    menuless ? 'extAgents-pill-fixed' : busy ? 'extAgents-pill-busy' : '',
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

/* The sentence for each kind of fix. `command` says whether a command follows
   it: a kind that may come without one has a bare spelling that ends the
   sentence instead of leading into a command that is not there. `then` says the
   command is only the way in and a step follows it. Every key is written out,
   so the catalogue gate reads each one. */
const FIX_SAY: Record<
  Remedy['kind'],
  (vars: { agent: string; button: string }, command: boolean, then: boolean, remedy: Remedy) => string
> = {
  sign_in: (v, command) => (command ? t('gui.agent.fix_sign_in', v) : t('gui.agent.fix_sign_in_bare', v)),
  setup: (v, command, then) =>
    !command ? t('gui.agent.fix_sign_in_bare', v) : then ? t('gui.agent.fix_setup_then', v) : t('gui.agent.fix_setup', v),
  api_key: (v) => t('gui.agent.fix_api_key', v),
  download: (v, command) => (command ? t('gui.agent.fix_download', v) : t('gui.agent.fix_download_bare', v)),
  model: (v, command) => (command ? t('gui.agent.fix_model', v) : t('gui.agent.fix_model_bare', v)),
  billing: (v, command) => (command ? t('gui.agent.fix_billing', v) : t('gui.agent.fix_billing_bare', v)),
  quota: (v, command) => (command ? t('gui.agent.fix_quota', v) : t('gui.agent.fix_quota_bare', v)),
  network: (v, command) => (command ? t('gui.agent.fix_network', v) : t('gui.agent.fix_network_bare', v)),
  silent: (v, command) => (command ? t('gui.agent.fix_silent', v) : t('gui.agent.fix_silent_bare', v)),
  upgrade: (v, command) => (command ? t('gui.agent.fix_upgrade', v) : t('gui.agent.fix_upgrade_bare', v)),
  exited: (v) => t('gui.agent.fix_exited', v),
  /* Both versions or neither: without them the sentence would name no Node.js,
     and what is left is a launch that quit. */
  runtime: (v, command, _then, r) =>
    !r.needs || !r.found
      ? t('gui.agent.fix_exited', v)
      : command
        ? t('gui.agent.fix_runtime', { ...v, needs: r.needs, found: r.found })
        : t('gui.agent.fix_runtime_bare', { ...v, needs: r.needs, found: r.found }),
  plan: (v, command) => (command ? t('gui.agent.fix_plan', v) : t('gui.agent.fix_plan_bare', v)),
  config: (v, command) => (command ? t('gui.agent.fix_config', v) : t('gui.agent.fix_config_bare', v)),
}

/* What to do about the last thing that went wrong, as the block at the top of
   the sheet's body draws it. The head only says what state the agent is in;
   this is the why and the how. */
interface NoteSpec {
  title: string
  /* How long ago, for a remembered test verdict. */
  when?: string
  lead: string
  command?: string
  /* What to type once `command` runs, when the fix is a step inside the agent:
     the note then draws two numbered steps, each with its own copy button. */
  then?: string
  /* The server's own sentence. Folded under the lead when the lead already
     explains it (a classified refusal); shown as it came when it is all there
     is to go on. */
  raw: string
  folded: boolean
}

/* A refusal the server classified: what is missing, in the reader's language,
   the command that supplies it on a line of its own, and the button to press
   after -- with the agent's own words folded under, since the sentence is
   English and written for a log. Null when the server named no fix. */
function remedied(agent: string, remedy: Remedy | null, button: string, raw: string): NoteSpec | null {
  if (!remedy) return null
  const command = remedy.kind === 'api_key' ? '' : remedy.command
  const then = command ? remedy.then || '' : ''
  const lead = FIX_SAY[remedy.kind]({ agent, button }, !!command, !!then, remedy)
  return { title: badOf(kindOf(remedy), agent), lead, command, then, raw, folded: true }
}

/* The block for this row, or none. A write this page refused comes first; then
   a test the server remembers failing, which the sheet reports from the
   unauthorized state too, since Test is offered there and its failure would
   otherwise leave no trace. Without a fix the reader is told in their own
   language what failed and what to press, and the server's sentence is shown
   as it came: it is the only reason there is, and folding it away made the
   block say nothing. */
/* The press the sheet's action bar offers after a failed test, which is the
   one its note has to name: read off the same branches as the bar in
   `AgentSheet`. A connected row retests; an unauthorized one tests, since that
   is how it earns its Connect back; any other row that is not connected offers
   Connect alone -- a key to fill in, or a handshake a test has since cleared --
   and connecting runs the same test. Empty where the bar offers nothing to
   press, which is Raven itself, whose rows are never tested. */
function testPress(row: ExtAgentRow, shown: Shown): string {
  if (shown === 'on') return canTest(row) ? t('gui.agent.test_again') : ''
  if (shown !== 'off') return ''
  return t(stageOf(row) === 'unauthorized' ? 'gui.agent.test_label' : 'gui.agent.connect')
}

function noteOf(row: ExtAgentRow, s: ExtAgentsState, shown: Shown): NoteSpec | null {
  const agent = row.name
  const failed = s.failed[row.name]
  if (failed) {
    const button = t('gui.retry')
    const spec = remedied(agent, failed.remedy || null, button, failed.detail)
    if (spec) return spec
    const write = refusedWrite(failed)
    return {
      title: write === 'save' ? t('gui.agent.bad_save') : t(write === 'disconnect' ? 'gui.agent.bad_disconnect' : 'gui.agent.bad_connect', { agent }),
      lead: write === 'save' ? t('gui.agent.said_save') : t(write === 'disconnect' ? 'gui.agent.said_disconnect' : 'gui.agent.said_connect', { button }),
      raw: failed.detail,
      folded: false,
    }
  }
  if (row.last_test_ok !== false) return null
  const button = testPress(row, shown)
  if (!button) return null
  const when = ago(row.last_test_at_ms)
  const spec = remedied(agent, row.last_test_remedy || null, button, row.last_test_detail || '')
  if (spec) return { ...spec, when }
  return { title: t('gui.agent.st_test_bad'), when, lead: t('gui.agent.said_test', { button }), raw: row.last_test_detail || '', folded: false }
}

function Note({ title, when, lead, command, then, raw, folded }: NoteSpec): JSX.Element {
  return (
    <div className="extAgents-note">
      <div className="extAgents-note-t">
        {title}
        {when ? <span className="extAgents-note-when">{when}</span> : null}
      </div>
      <div className="extAgents-note-p">{lead}</div>
      {command && then ? (
        <ol className="extAgents-steps">
          <li>
            {t('gui.agent.fix_step_run')}
            <CmdCopy cmd={command} />
          </li>
          <li>
            {t('gui.agent.fix_step_type')}
            <CmdCopy cmd={then} />
          </li>
        </ol>
      ) : command ? (
        <CmdCopy cmd={command} />
      ) : null}
      {!raw ? null : folded ? (
        <details className="extAgents-note-raw">
          <summary>{t('gui.agent.fix_raw')}</summary>
          <pre>{raw}</pre>
        </details>
      ) : (
        <pre className="extAgents-note-said">{raw}</pre>
      )}
    </div>
  )
}

/* One line under the name in the sheet: `healthOf`'s verdict, drawn with the
   room a sheet has -- a refusal with its fix, a caveat with the probe's own
   words folded under it -- or who makes the agent when there is nothing to say
   about it. */
function StatusLine({ row, shown, s }: { row: ExtAgentRow; shown: Shown; s: ExtAgentsState }): JSX.Element {
  const by = byOf(row)
  const health = healthOf(row, s)
  const led = <span aria-hidden="true" className={ledClass(health.tone)} />
  if (health.tone === 'busy') {
    return (
      <div className="extAgents-by">
        <Spin />
        {health.label}
      </div>
    )
  }
  /* The head says only which state: the why and the how are the block at the
     top of the body (`noteOf`). */
  if (health.from === 'write') {
    const failed = s.failed[row.name]
    return (
      <div className="extAgents-by extAgents-by-bad">
        {led}
        {[failed ? refusedLabel(failed) : t('gui.agent.st_connect_bad'), by].filter(Boolean).join(' · ')}
      </div>
    )
  }
  if (health.from === 'test') {
    return (
      <div className="extAgents-by extAgents-by-bad">
        {led}
        {shown === 'on' ? t('gui.agent.hd_on_test_bad') : [t('gui.agent.st_test_bad'), by].filter(Boolean).join(' · ')}
      </div>
    )
  }
  /* The probe's sentence is English and written for a log, so it is folded
     under the line the way a refusal's is, not put in it. */
  if (health.tone === 'warn') {
    return (
      <div className="extAgents-by extAgents-by-fix">
        {led}
        <div className="extAgents-fix">
          <div>{health.label}</div>
          {row.probe_detail ? (
            <details className="extAgents-raw">
              <summary>{t('gui.agent.probe_raw')}</summary>
              {row.probe_detail}
            </details>
          ) : null}
        </div>
      </div>
    )
  }
  if (health.tone === 'good') {
    return (
      <div className="extAgents-by">
        {led}
        {health.label}
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
  const note = noteOf(row, s, shown)
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
        {/* Raven's shipped specialists are part of Raven, not something the
            reader connected, so there is nothing to disconnect. */}
        {row.vendored ? null : (
          <button className="mini danger" onClick={() => store.disconnectRow(row)}>
            {t('gui.agent.disconnect')}
          </button>
        )}
        {canTest(row) && testing ? (
          <button className="mini danger" onClick={() => store.stopTest(row)}>
            <Spin />
            {t('gui.stop')}
          </button>
        ) : canTest(row) ? (
          <button className="mini" onClick={() => void store.runTest(row)}>
            {t(row.last_test_ok === false ? 'gui.agent.test_again' : 'gui.agent.test_label')}
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
            {note ? <Note {...note} /> : null}
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

type Tab = 'all' | Section

const TABS: Array<{ tab: Tab; label: string; empty: string }> = [
  { tab: 'all', label: 'gui.filter.all', empty: 'gui.agent.none' },
  { tab: 'on', label: 'gui.agent.g_on', empty: 'gui.agent.none' },
  { tab: 'avail', label: 'gui.agent.g_avail', empty: 'gui.agent.none_avail' },
  { tab: 'missing', label: 'gui.agent.g_missing', empty: 'gui.agent.none_missing' },
]

export function ExtAgentsApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* The language the page resolved, so a pick repaints this island: every word
     below is a t(key) read at render time (state/lang/store.ts). */
  useSyncExternalStore(lang.subscribe, lang.get)
  const [tab, setTab] = useState<Tab>('all')
  const by = (section: Section): ExtAgentRow[] => ordered(s.rows.filter((row) => sectionOf(row) === section))
  const rows: Record<Tab, ExtAgentRow[]> = { all: [], on: by('on'), avail: by('avail'), missing: by('missing') }
  rows.all = [...rows.on, ...rows.avail, ...rows.missing]
  const current = TABS.find((x) => x.tab === tab)!
  /* Before the first answer only: a later reload keeps showing the rows it
     has, the way the wizard's step does. */
  const scanning = s.loading && s.rows.length === 0
  const sheetRow = s.sheet ? s.rows.find((x) => x.name === s.sheet) : undefined
  return (
    <>
      <div className="pmhero">
        <div>
          <h3>{t('gui.page.agents')}</h3>
          <p>{t('gui.page.agents_sub')}</p>
        </div>
      </div>
      {/* All and Connected are always offered; the other two only with agents in
          them, or while they are the tab being read -- so connecting the last
          one leaves the reader on an emptied tab rather than moving them. */}
      <div className="extAgents-tabs" role="tablist">
        {TABS.filter((x) => x.tab === 'all' || x.tab === 'on' || x.tab === tab || rows[x.tab].length).map((x) => (
          <button
            aria-selected={tab === x.tab}
            className="extAgents-tab"
            key={x.tab}
            onClick={() => setTab(x.tab)}
            role="tab"
            type="button"
          >
            {t(x.label)}
            {scanning ? <span className="extAgents-wbar extAgents-wtn" /> : <span className="extAgents-tn">{String(rows[x.tab].length)}</span>}
          </button>
        ))}
      </div>
      {scanning ? <WaitGrid /> : <CardGrid empty={t(current.empty)} rows={rows[tab]} s={s} />}
      {sheetRow ? <AgentSheet key={`${s.sheet}:${s.epoch}`} row={sheetRow} s={s} /> : null}
    </>
  )
}
