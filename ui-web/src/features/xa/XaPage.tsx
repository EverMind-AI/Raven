import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useSyncExternalStore } from 'react'

import { AgentMark } from '../../shell/agent-mark'
import { shell, t } from '../../shell/bridge'
import { KeyInput } from '../../shell/key-input'
import { SetupGroup, SetupRow } from '../../shell/setuprow'
import * as store from './store'

import type { XaRow } from './types'
import type { JSX } from 'react'

/* Connect the agents this machine can hand work to. One row per agent; the rows
   are whatever `DS.xa` answers -- the fixture source with no gateway behind the
   page, the `subagents.*` source in the live layer.
 *
 * Two verbs on a row, and only two: connect and disconnect. Connect does whatever
 * this particular agent needs to become dispatchable -- build a shipped folder's
 * venv and its dependencies, write a config entry from a preset, take a
 * credential, or just flip the roster switch back on -- and disconnect only marks
 * it unavailable in the registry, so it is one click away from working again. What
 * used to be here instead was the mechanism, spread across five buttons
 * (install, connect, enable, test, switch to) that each named a step of the same
 * errand and left the reader to sequence them. Test is back, but not as a step of
 * that errand and not on a row: it is a question about one agent, asked inside the
 * card that agent opens.
 *
 * The rows carry no descriptions. They are the preset's own prompt text, written
 * for the model that reads it when choosing whom to delegate to -- printing it
 * under every row put sentences like "IMPORTANT: it cannot call sub-agents" on
 * screen as if they were help. It appears once, in the card the row opens.
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

/* How Raven reaches this agent, which is the one technical fact about it worth
   printing: a subprocess, a protocol, an HTTP endpoint, or this process itself.
   Deliberately not where the agent came from: whether an install shipped it or
   a reader connected it changes nothing about using it, and the row and the card
   are both about using it. */
function wayIn(row: XaRow): string {
  return [
    kindText(row.kind),
    /* The preset moved to another transport since this entry was written.
       Rewriting it silently would change its command line and invalidate every
       session handle bound to it, so it is stated, and disconnect-then-connect
       is what re-reads the preset. */
    row.upgrade_to ? t('gui.agent.stale_to', { to: kindText(row.upgrade_to) }) : '',
  ]
    .filter(Boolean)
    .join(' · ')
}

/* The dot beside the name, and nothing under it. Health is a colour here, not a
   sentence: the group heading says whether the agent is connected, the button
   says what to do about it, and a third line repeating either in grey was the
   standing small print this page was cleared of.
 *
 * Four probe verdicts, and they are not two. `attention` means the binary is
 * there but nothing has verified it can do a task -- an amber nudge, not a
 * failure. `unknown` is "not measured", which earns no colour at all. */
function dotOf(row: XaRow): string {
  /* A built-in agent is this process: reading it through the probe verdicts
     would report "not measured" about a loop that is demonstrably running. */
  if (row.builtin) return row.enabled ? 'ok' : 'off'
  if (row.building || row.test_running) return 'warn'
  if (row.kind === 'openai' && !row.has_api_key) return 'bad'
  if (row.configured && !row.enabled && row.probe_status !== 'missing') return 'off'
  if (row.probe_status === 'ready') return 'ok'
  if (row.probe_status === 'attention') return 'warn'
  if (row.probe_status === 'unknown') return 'off'
  return 'bad'
}

/* What connecting this row still has to do. One stage, one write -- the page's
   whole decision, so the row and the card cannot offer different verbs for the
   same state.
 *
 * `install` and `off` both describe a shipped folder that is not on the roster,
 * and they are not the same job: a folder whose venv was never built needs the
 * installer (minutes, hundreds of MB), while one that was switched off needs
 * its manifest flag back. The probe verdict is what separates them. */
export type Stage = 'builtin' | 'building' | 'install' | 'add' | 'key' | 'stale' | 'off' | 'live'

export function stageOf(row: XaRow): Stage {
  if (row.builtin) return 'builtin'
  if (row.building) return 'building'
  if (row.vendored) {
    if (row.probe_status === 'missing') return 'install'
    return row.enabled ? 'live' : 'off'
  }
  /* An HTTP agent cannot answer without its key, so it is not connected by
     writing an entry -- the key is the missing part, whether the entry exists
     yet or not. */
  if (row.kind === 'openai' && !row.has_api_key) return 'key'
  if (!row.configured) return 'add'
  if (row.enabled) return 'live'
  /* Out of service and its preset has moved to another transport. Connecting it
     is a remove plus an add, not a flag, so it is its own stage rather than a
     variant of `off` -- the flag left `upgrade_to` standing and the old command
     line in place, which is an agent the card offered to migrate and never did.
     After `live`, so an agent still in service keeps offering the one verb its
     state calls for, which is disconnect. */
  if (row.upgrade_to) return 'stale'
  return 'off'
}

/* Connect, disconnect, or nothing -- the same component in the row and in the
   card, so the two can never disagree about what this agent needs next.
 *
 * `key` is the one stage whose write cannot be done from here: only the reader
 * has the credential. In the row it opens the card, where the field is; in the
 * card it is the field's own button, which is why the card passes `onKey`. */
function AgentAct({ row, onKey }: { row: XaRow; onKey?: () => void }): JSX.Element | null {
  const stage = stageOf(row)
  if (stage === 'builtin') return null
  if (stage === 'building') {
    return (
      <button className="mini" disabled title={t('gui.agent.install_note')}>
        {t('gui.agent.installing')}
      </button>
    )
  }
  if (stage === 'live') {
    /* No confirm: this only marks it unavailable in the registry -- the entry,
       the folder and the sessions it already ran all stay, and connect puts it
       back. A dialog would be asking permission for a switch. */
    return (
      <button className="mini ghost" onClick={() => void store.run('toggle', row, { enabled: false })}>
        {t('gui.agent.disconnect')}
      </button>
    )
  }
  const connect = (): void => {
    if (stage === 'install') void store.run('build', row, {})
    else if (stage === 'add') void store.run('connect', row, {})
    else if (stage === 'stale') {
      /* The one connect that is not a flag. A preset that changed transport
         cannot be applied by switching `enabled`: there is no write that
         changes a transport, so the entry is removed and added back from the
         preset -- which drops the handles of runs already in flight, and is
         why this one asks first. */
      shell().confirmAsk?.(
        t('gui.agent.migrate_do'),
        t('gui.agent.migrate_body', { name: row.name, to: kindText(row.upgrade_to || '') }),
        t('gui.agent.migrate_do'),
        () => void store.run('migrate', row, {}),
      )
    } else if (stage === 'off') void store.run('toggle', row, { enabled: true })
    else if (onKey) onKey()
    else store.sheetOpen(row)
  }
  return (
    <button
      className="mini"
      /* The one connect that costs the reader something to know about before
         they click it. */
      title={stage === 'install' ? t('gui.agent.install_note') : undefined}
      onClick={connect}
    >
      {t('gui.agent.connect')}
    </button>
  )
}

/* Which group a row belongs in: two verbs, two groups. A row that is dispatchable
   sits under "on duty" and offers disconnect; everything else is addable and
   offers connect. The page used to lead with provenance -- built-in, then
   vendored, then connected, then available -- which put a broken agent three
   groups down while a healthy built-in row sat at the top with nothing to do. */
type Grp = 'on' | 'off'
const groupOf = (row: XaRow): Grp => {
  const stage = stageOf(row)
  return stage === 'live' || stage === 'builtin' ? 'on' : 'off'
}

/* What connecting costs, which is what the addable group is ordered by -- the
   same question the entrances page sorts on. A switch is instant, an entry is a
   file write, a credential needs the reader to go and find one, and an install
   is several hundred megabytes. */
const costOf = (row: XaRow): number => {
  const stage = stageOf(row)
  return stage === 'off' ? 0 : stage === 'add' ? 1 : stage === 'key' ? 2 : 3
}

/* Whether `subagents.test` can answer for this row at all.

   Two rows it cannot. A built-in agent is this process, and `run_test` refuses
   one outright -- "there is nothing to test". A discovered one is looked up by
   neither name the call accepts: `source: "preset"` searches the preset table
   it was never in, and `source: "config"` searches a config file it has no
   entry in, so both answer `subagent_not_found`. Offering the button there
   would be offering a click that can only fail. */
const canTest = (row: XaRow): boolean => !row.builtin && !row.vendored

/* How long ago, in the coarsest unit that still says it. Same thresholds as the
   TUI's roster (ui-tui/src/components/subagentsHub.tsx `ageText`), spelled
   again rather than imported across the two apps -- and through t(), because a
   bare "3h" beside a Chinese sentence is the one part of the line that stays
   English. */
function agoText(ms: number): string {
  const mins = Math.max(0, Math.round((Date.now() - ms) / 60000))
  if (mins < 1) return t('gui.time.ago_now')
  if (mins < 60) return t('gui.time.ago_m', { n: mins })
  const hours = Math.round(mins / 60)
  if (hours < 24) return t('gui.time.ago_h', { n: hours })
  return t('gui.time.ago_d', { n: Math.round(hours / 24) })
}

/* The last verdict as one sentence, and the dot beside it.

   A verdict with no time on it reads as untested rather than as a verdict with
   a hole in it: the server writes `ok` and `tested_at_ms` from one record, so
   the two are either both there or both absent, and printing "Worked, {ago}"
   with the placeholder still in it is worse than saying nothing was measured. */
function testVerdict(row: XaRow): { cls: string; text: string } {
  if (row.last_test_ok == null || row.last_test_at_ms == null) {
    return { cls: 'off', text: t('gui.agent.test_never') }
  }
  const ago = agoText(row.last_test_at_ms)
  return row.last_test_ok
    ? { cls: '', text: t('gui.agent.test_ok', { ago }) }
    : { cls: 'bad', text: t('gui.agent.test_bad', { ago }) }
}

/* Does running this one cost the reader anything? Only a cli test dispatches a
   real task; acp reaches its verdict in the handshake and openai in the free
   `/models` probe, and warning about a bill neither of them sends is how a
   reader learns to ignore the warning. */
const testCosts = (row: XaRow): boolean => row.kind === 'cli'

function AgentRow({ row, sel }: { row: XaRow; sel: boolean }): JSX.Element {
  /* Health is only a question about an agent that is supposed to be working.
     In the addable group not-dispatchable is what every row is, so a red dot and
     the clay stripe that comes with it were an alarm about the group's own
     definition -- and with the status line gone there was nothing left to say
     what the alarm meant. */
  const cls = groupOf(row) === 'on' ? dotOf(row) : 'off'
  return (
    <SetupRow
      name={row.name}
      /* The brand of the package behind the row, not an initial taken off its
         name: the name is the reader's to change, and the two need not agree. */
      tile={<AgentMark preset={row.preset} />}
      /* Empty text on purpose: SetupRow draws the second line only when there
         is something to say there, and here there is not. */
      state={{ cls, text: '' }}
      tags={<span className="kd">{kindText(row.kind)}</span>}
      act={<AgentAct row={row} />}
      sel={sel}
      onOpen={() => store.sheetOpen(row)}
    />
  )
}

/* A fact on the card the reader owns, edited where it is read.

   Two of the three things this card says are the reader's: what this agent is
   called here, and what it is for. The rest -- how Raven reaches it, whether it
   answered -- belongs to the transport and to the agent. `subagents.update` has
   always taken both, `DS.xa` has always forwarded both, and the store has always
   moved an open sheet onto a new name; the page was the only piece missing, and
   it went out with the form this card replaced.

   Not that form back. There is no second field to fill in and no save button to
   find: the text is the control, Enter commits and Escape restores. Blur commits
   too -- clicking away from a field you have just typed into means the typing,
   and the alternative is a card that discards work whenever the reader reaches
   for something else. In the multi-line one Enter is a newline, so there the
   blur is the only commit. */
function Editable({
  value,
  multiline,
  placeholder,
  label,
  onCommit,
}: {
  value: string
  multiline?: boolean
  placeholder: string
  label: string
  onCommit: (next: string) => void
}): JSX.Element {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value)
  /* The rows are re-read after every write, and a rename lands as a new value
     for this same field. Without this the box would go on showing what was
     typed even where the server settled on something else. */
  useEffect(() => setDraft(value), [value])

  if (!editing) {
    return (
      <button className="xaedit" type="button" aria-label={label} title={label} onClick={() => setEditing(true)}>
        {value || <span className="none">{placeholder}</span>}
      </button>
    )
  }

  const commit = (): void => {
    setEditing(false)
    const next = multiline ? draft : draft.trim()
    if (next !== value) onCommit(next)
  }
  const onKeyDown = (event: { key: string; preventDefault: () => void }): void => {
    if (event.key === 'Escape') {
      event.preventDefault()
      setEditing(false)
      setDraft(value)
    } else if (event.key === 'Enter' && !multiline) {
      event.preventDefault()
      commit()
    }
  }
  return multiline ? (
    <textarea
      autoFocus
      aria-label={label}
      className="xaedit on"
      onBlur={commit}
      onChange={(e) => setDraft(e.target.value)}
      onKeyDown={onKeyDown}
      rows={3}
      value={draft}
    />
  ) : (
    <input
      autoFocus
      aria-label={label}
      className="xaedit on"
      onBlur={commit}
      onChange={(e) => setDraft(e.target.value)}
      onKeyDown={onKeyDown}
      type="text"
      value={draft}
    />
  )
}

/* One agent's card, drawn into the same #detail dialog the skill and plugin
   details use -- and with their anatomy, not one of its own: identity block,
   then a described section. Three things about the agent (who it is, what it is
   good at, how Raven reaches it) and the one verb its state calls for.
 *
 * What it is not any more: a form. It carried editable name and description
 * fields, a status line with a second copy of the row's own sentence, a test
 * verdict, a transport-migration offer and a save button -- five things to read
 * before the one thing to do. The verdict is back, but as a section of its own
 * with the button that renews it: it was noise as a line in a form nobody had
 * asked a question of, and it is the answer once somebody asks. */
function AgentCard({ row, testing }: { row: XaRow; testing: boolean }): JSX.Element {
  const dHost = store.detailHost()
  const keyRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    const title = document.getElementById('dTitle')
    if (title) title.textContent = ''
    const drawer = document.getElementById('detail')
    if (drawer) drawer.dataset.open = 'true'
  }, [row])
  const stage = stageOf(row)
  /* An entry that exists takes the key as an edit; one that does not is written
     from its preset with the key in hand. Either way the reader typed one thing
     and the agent is connected after it. */
  const saveKey = (): void => {
    const api_key = keyRef.current ? keyRef.current.value.trim() : ''
    if (!api_key) return
    void store.run(row.configured ? 'update' : 'connect', row, { api_key })
  }
  /* An agent with an entry to write. A discovered folder has none yet and
     `subagents.update` writes one from its own manifest, which is why it counts.
     A preset nobody has connected has nothing to update, and making the fields
     live there would leave a typed name either vanishing or -- worse --
     connecting the agent, which is not what typing a name asks for. Connect
     first and describe after: neither field is out of reach either way. */
  const owned = row.configured || !!row.vendored
  const verdict = testVerdict(row)
  /* Two ways to be running, and both count: this page started one and is
     holding the call open, or the rows came back saying somebody else's is
     still in flight. Reading only the row flag left the button live for the
     whole two minutes of the test this very card started. */
  const running = testing || row.test_running
  /* And the name is narrower than the description. `subagents.update` refuses a
     rename on a row it had to materialise from a shipped launcher -- "its name
     binds it to the shipped launcher" -- so offering the control there would be
     offering a write the server always refuses. The description has no such
     rule and writes the entry, after which the row is configured and its name
     is the reader's like any other.

     Narrower again while a test runs, and this one is not about the write: the
     name is the only handle either side keeps on the run. The server registers
     the task under it (`_RUNNING[name]`, which is what `subagents.test_cancel`
     looks in) and records the verdict against it. Rename mid-run and the row
     the card is now showing matches neither -- Stop disappears, Run comes back
     and starts a *second* test under the new name, and the first goes on for
     up to two minutes with nothing pointing at it and a verdict nothing will
     display. Holding the name for the length of the test is the whole fix; the
     description keys nothing and stays live. */
  const renamable = row.configured && !running
  const edit = (patch: { description?: string; new_name?: string }): void => {
    void store.run('update', row, patch)
  }
  return createPortal(
    <>
      <div className="pmdhead">
        <AgentMark preset={row.preset} />
        <div className="pmdmeta">
          <div className="l1">
            <b>
              {renamable ? (
                <Editable
                  label={t('gui.agent.rename')}
                  onCommit={(next) => {
                    if (next) edit({ new_name: next })
                  }}
                  placeholder={row.name}
                  value={row.name}
                />
              ) : (
                row.name
              )}
            </b>
          </div>
          <div className="l2">{wayIn(row)}</div>
        </div>
        {/* The key stage's button belongs beside its field, not up here where
            there is nothing to type into. */}
        <div className="dact">{stage === 'key' ? null : <AgentAct row={row} />}</div>
      </div>
      {/* An owned agent gets the section whether or not it has a description:
          the empty one is where the reader writes the first. An unowned row
          keeps the old rule -- a heading over nothing is a heading about
          nothing. */}
      {owned || row.description ? (
        <div className="pmsec">
          <div className="cap">{t('gui.plug.sec_about')}</div>
          <div className="pmdesc">
            {owned ? (
              <Editable
                label={t('gui.agent.redescribe')}
                multiline
                onCommit={(next) => edit({ description: next })}
                placeholder={t('gui.agent.about_none')}
                value={row.description || ''}
              />
            ) : (
              row.description
            )}
          </div>
        </div>
      ) : null}
      {/* Whether it works, which is the one thing about an agent this page
          could never say: the probe answers "the command is on the machine",
          and the reader wanting to know whether it can be delegated to had to
          find out by delegating. Its own section, below the description and
          not beside the connect button, because it is not a step of connecting
          -- a connected agent is the one most worth asking about. */}
      {canTest(row) ? (
        <div className="pmsec">
          <div className="cap">{t('gui.agent.sec_test')}</div>
          <div className="sutest">
            <span className={verdict.cls ? `led ${verdict.cls}` : 'led'} />
            <span className="vd">{verdict.text}</span>
            {/* Stop is offered only while one is running, and it is the ghost
                of the pair: the reader who opened this section came to test. */}
            {running ? (
              <button className="mini ghost" onClick={() => store.stopTest(row)}>
                {t('gui.agent.test_stop')}
              </button>
            ) : null}
            <button className="mini" disabled={running} onClick={() => void store.runTest(row)}>
              {running ? t('gui.agent.test_running') : t('gui.agent.test_do')}
            </button>
          </div>
          {/* The detail is worth the line only when it is a reason. A passing
              cli test says "the agent ran and replied", which the verdict above
              already said in fewer words. */}
          {row.last_test_ok === false && row.last_test_detail ? (
            <div className="pmdesc">{row.last_test_detail}</div>
          ) : null}
          {testCosts(row) ? <div className="pmdesc">{t('gui.agent.test_note')}</div> : null}
        </div>
      ) : null}
      {stage === 'key' ? (
        <div className="pmsec">
          <div className="cap">{t('gui.agent.key')}</div>
          <div className="sukey">
            <KeyInput
              placeholder={row.has_api_key ? t('gui.agent.key_set') : ''}
              aria-label={t('gui.agent.key')}
              ref={keyRef}
              onKeyDown={(e) => {
                if (e.key === 'Enter') saveKey()
              }}
            />
            <button className="mini key" onClick={saveKey}>
              {t('gui.agent.connect')}
            </button>
          </div>
        </div>
      ) : null}
    </>,
    dHost,
  )
}

export function XaApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  /* Legacy chrome owns the drawer's closers (Esc, #dClose, click-outside)
     and they only flip #detail's data-open, so the island follows the flag
     to unmount its portal before another page's opener wipes #dBody. */
  useEffect(() => {
    const el = document.getElementById('detail')
    if (!el) return
    const ob = new MutationObserver(() => {
      if (el.dataset.open !== 'true') store.sheetDismissed()
    })
    ob.observe(el, { attributes: true, attributeFilter: ['data-open'] })
    return () => ob.disconnect()
  }, [])
  const on = s.rows.filter((a) => groupOf(a) === 'on')
  const off = s.rows.filter((a) => groupOf(a) === 'off').sort((a, b) => costOf(a) - costOf(b))
  const sheetRow = s.sheet ? s.rows.find((x) => x.name === s.sheet) : undefined
  const rows = (list: XaRow[]): JSX.Element => (
    <div className="sulist">
      {list.map((row) => (
        <AgentRow key={row.name} row={row} sel={s.sheet === row.name} />
      ))}
    </div>
  )
  return (
    <>
      <div className="pmhero">
        <h3>{t('gui.page.agents')}</h3>
      </div>
      {/* A heading with nothing under it is a heading about nothing: each group
          appears only when it has rows. The ordering inside the addable one is
          still by what connecting costs -- that is behaviour, and it does not
          need a caption to be true. */}
      {on.length ? (
        <SetupGroup label={t('gui.agent.g_on')} count={on.length}>
          {rows(on)}
        </SetupGroup>
      ) : null}
      {off.length ? (
        <SetupGroup label={t('gui.agent.g_off')} count={off.length}>
          {rows(off)}
        </SetupGroup>
      ) : null}
      {sheetRow ? (
        <AgentCard key={`${s.sheet}:${s.epoch}`} row={sheetRow} testing={s.testing.includes(sheetRow.name)} />
      ) : null}
    </>
  )
}
