/* The machines tab of the Agent Hub: a card per registered machine, the sheet
 * a card opens, and the form that registers one.
 *
 * Drawn with the agents' own pieces -- the same card, tabs, fields and action
 * bar -- so the tab reads as one more answer to "where can my work go", not as
 * a settings page. A card says who the machine is (its glyph, name, id), how
 * it is doing (the link's dot, the node's version) and the one thing to do
 * about it now (install or update the node). Everything else is in the sheet:
 * the directories it reads from without asking, what the doctor says, and the
 * actions, removal among them.
 *
 * Nothing here shows an address, a key path or a password: the server hands
 * over what a machine is, never the way onto it, and the register form's
 * fields are written once and not read back. A refusal comes with its kind, and the sheet says in the
 * reader's language what to do about it, with the server's sentence under it.
 */

import { Fragment, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { Glyph } from '../../components/Ico'
import { t } from '../../i18n/t'
import { ask as confirmAsk } from '../../state/confirm'
import { show as toast } from '../../state/toast'
import * as machines from './machines'
import { Spin, WaitGrid } from './Rows'
import * as store from './store'

import type { MachinesState } from './machines'
import type { ExtAgentsState } from './store'
import type { MachineLink, MachineRow, RegisterForm, RegisterReason } from './types'
import type { JSX } from 'react'

const MACHINE = 'M4 5.5h16v9.5H4zM9.5 19h5M12 15.5V19'
const PLUS = 'M12 5v14M5 12h14'

/* What to do about a refusal, by its kind: the page's own words, with the
   server's sentence shown under them for the reader who wants ssh's words. */
const WHY: Record<Exclude<RegisterReason, ''>, string> = {
  unreachable: 'gui.machine.why_unreachable',
  login: 'gui.machine.why_login',
  hostkey: 'gui.machine.why_hostkey',
  timeout: 'gui.machine.why_timeout',
  nossh: 'gui.machine.why_nossh',
  platform: 'gui.machine.why_platform',
  duplicate: 'gui.machine.why_duplicate',
  invalid: 'gui.machine.why_invalid',
  registry: 'gui.machine.why_registry',
}

function MachineTile(): JSX.Element {
  return (
    <span className="extAgents-tile extAgents-mtile">
      <Glyph d={MACHINE} />
    </span>
  )
}

/* The link's dot: only there when there is something to say, as on an agent
   card -- a link that is up, down, or in error; a machine nobody has reached
   yet carries none. */
const LED: Record<MachineLink, string> = {
  connected: 'extAgents-led',
  idle: 'extAgents-led',
  lost: 'extAgents-led extAgents-led-warn',
  error: 'extAgents-led extAgents-led-bad',
  off: '',
}

function LinkLed({ row }: { row: MachineRow }): JSX.Element | null {
  if (!LED[row.state]) return null
  return <span className={LED[row.state]} />
}

/* The two refusals a reader can act on from this page, said with what to
   do; any other error is the pool's own sentence, which names the next step. */
function errorLine(said: string): string {
  if (/no Raven node on machine/.test(said)) return t('gui.machine.err_no_node', { said })
  if (/runs different code from this Raven/.test(said)) return t('gui.machine.err_stale_node', { said })
  return said || t('gui.machine.state_error')
}

export function stateLine(row: MachineRow): string {
  if (row.state === 'connected') return t('gui.machine.state_connected', { n: String(row.agents) })
  if (row.state === 'idle') return t('gui.machine.state_idle')
  if (row.state === 'lost') return row.detail || t('gui.machine.state_lost')
  if (row.state === 'error') return errorLine(row.detail)
  return row.transport === 'local' ? t('gui.machine.transport_local') : t('gui.machine.state_off')
}

const installing = (row: MachineRow, ms: MachinesState): boolean => ms.busy[row.id] === 'install' || row.install.running

/* A node to put there, or to update: an ssh machine whose node is not this
   Raven's code. This computer has none to install. */
const needsNode = (row: MachineRow): boolean => row.transport === 'ssh' && !row.node_current

function nodeLine(row: MachineRow, ms: MachinesState): string {
  if (installing(row, ms)) return t('gui.machine.installing')
  if (row.transport === 'local') return t('gui.machine.node_here')
  if (row.node_current) return t('gui.machine.node_version', { v: row.node_version })
  if (row.node_version) return t('gui.machine.node_stale', { v: row.node_version })
  return t('gui.machine.node_none')
}

const installLabel = (row: MachineRow): string => t(row.node_version ? 'gui.machine.update' : 'gui.machine.install')

/* Which connected agent entries run on the machine. The line a reader whose
   machine is up and idle was missing: "no agent running" says what the link
   is doing now, this says whether anything is connected here at all. */
/* Every entry on the machine, the switched-off ones marked: those still hold
   the machine for the add, so a card that counted only the connected ones
   said "no agent connected here" beside a form that greyed the preset. */
const agentsHere = (row: MachineRow): string[] => [
  ...row.agents_here,
  ...row.agents_off_here.map((name) => t('gui.machine.off_suffix', { name })),
]

function agentsLine(row: MachineRow): string {
  const names = agentsHere(row)
  return names.length
    ? t('gui.machine.agents_line', { n: String(names.length), names: names.join(', ') })
    : t('gui.machine.agents_line_none')
}

/* A machine an agent can be connected on from its sheet: reached over ssh,
   with this Raven's node on it, and not mid-install. */
const canHost = (row: MachineRow, ms: MachinesState): boolean =>
  row.transport === 'ssh' && row.node_current && !installing(row, ms)

function MachineCard({ row, ms }: { row: MachineRow; ms: MachinesState }): JSX.Element {
  const open = (): void => store.machineSheetOpen(row.id)
  const failed = ms.failed[row.id]
  const busy = installing(row, ms)
  return (
    <div
      className="extAgents-card"
      role="button"
      tabIndex={0}
      aria-current={store.get().msheet === row.id ? 'true' : undefined}
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
        <MachineTile />
        <div className="extAgents-nm">
          <span className="extAgents-t">{row.name}</span>
          <LinkLed row={row} />
        </div>
        <div className="extAgents-ctl" onClick={(e) => e.stopPropagation()}>
          {busy ? (
            <span className="extAgents-ctag">
              <Spin /> {t('gui.machine.installing')}
            </span>
          ) : needsNode(row) ? (
            <button className="mini" onClick={() => void machines.installNode(row.id)}>
              {installLabel(row)}
            </button>
          ) : null}
        </div>
      </div>
      <div className="extAgents-one">
        <span className="extAgents-mc">
          <Glyph d={MACHINE} />
          {row.id}
        </span>
        {nodeLine(row, ms)}
      </div>
      {row.transport === 'ssh' ? <div className="extAgents-one">{agentsLine(row)}</div> : null}
      <div className="extAgents-foot-slot">
        <div className={'extAgents-foot ' + (failed ? 'extAgents-foot-busy' : 'extAgents-foot-quiet')}>
          <span className={failed ? 'extAgents-foot-k' : 'extAgents-foot-say'}>{failed || stateLine(row)}</span>
        </div>
      </div>
    </div>
  )
}

function RegisterCard(): JSX.Element {
  return (
    <button className="extAgents-card extAgents-card-new" onClick={() => store.machineSheetOpen('new')} type="button">
      <span className="extAgents-tile extAgents-mtile extAgents-mtile-new">
        <Glyph d={PLUS} />
      </span>
      <span className="extAgents-t">{t('gui.machine.register')}</span>
    </button>
  )
}

export function MachinesGrid({ ms }: { ms: MachinesState }): JSX.Element {
  if (!ms.loaded && ms.loading) return <WaitGrid />
  /* What the doctor says about the registry file itself, which no card can carry. */
  const fileProblems = (ms.problems ?? []).filter((p) => !p.id)
  return (
    <>
      {ms.loadFailed ? (
        <div className="extAgents-note extAgents-note-warn">
          <div className="extAgents-note-t">{t('gui.machine.load_bad')}</div>
          <div className="extAgents-note-said">{ms.loadFailed}</div>
        </div>
      ) : null}
      {fileProblems.length ? (
        <div className="extAgents-note extAgents-note-warn">
          <div className="extAgents-note-t">{t('gui.machine.file_problems')}</div>
          <div className="extAgents-note-said">{fileProblems.map((p) => p.detail).join('\n')}</div>
        </div>
      ) : null}
      {ms.rows.length === 0 ? <div className="extAgents-empty">{t('gui.machine.none')}</div> : null}
      <div className="extAgents-grid">
        {ms.rows.map((row) => (
          <MachineCard key={row.id} ms={ms} row={row} />
        ))}
        <RegisterCard />
      </div>
    </>
  )
}

function Field({ k, children }: { k: string; children: JSX.Element | string }): JSX.Element {
  return (
    <div className="extAgents-fld">
      <span className="extAgents-k">{k}</span>
      <p className="extAgents-ro">{children}</p>
    </div>
  )
}

export function MachineSheet({ row, ms }: { row: MachineRow; ms: MachinesState }): JSX.Element {
  const dHost = store.detailHost()
  const dirRef = useRef<HTMLInputElement>(null)
  const busy = installing(row, ms)
  const probing = ms.busy[row.id] === 'probe'
  const removing = ms.busy[row.id] === 'remove'
  const failed = ms.failed[row.id]
  const bad = !!failed || row.state === 'error'
  const problems = (ms.problems ?? []).filter((p) => p.id === row.id)
  const shownProblems = ms.problems ? problems.map((p) => p.detail) : row.problems
  const hosts = canHost(row, ms)
  const install = (): void => void machines.installNode(row.id, dirRef.current?.value.trim() || '')
  const remove = (): void =>
    confirmAsk(
      t('gui.machine.remove_do'),
      t(row.auth === 'password' ? 'gui.machine.remove_body_password' : 'gui.machine.remove_body', { name: row.name, id: row.id }),
      t('gui.machine.remove_do'),
      () => {
        void machines.remove(row.id).then((gone) => {
          if (gone) store.closeSheet()
        })
      },
    )
  return createPortal(
    <div className="extAgents-sheet" aria-label={row.name} role="document">
      <div className="extAgents-head">
        <MachineTile />
        <div className="extAgents-meta">
          <h3>{row.name}</h3>
          <div className={'extAgents-by' + (bad ? ' extAgents-by-bad' : '')}>
            <LinkLed row={row} />
            {removing ? t('gui.machine.removing') : failed || stateLine(row)}
          </div>
        </div>
      </div>
      <div className="extAgents-body">
        {row.install.ok === false && !busy ? (
          <div className="extAgents-note extAgents-note-warn">
            <div className="extAgents-note-t">{t('gui.machine.install_bad')}</div>
            <div className="extAgents-note-said">{row.install.detail}</div>
            <div className="extAgents-about">{t('gui.machine.install_bad_hint')}</div>
          </div>
        ) : null}
        {hosts ? (
          /* The next step, said where the reader is: a machine with its node
             up has nothing to do on this tab, and the agents tab does not
             mention it until something runs here. */
          <div className="extAgents-note extAgents-note-next">
            <div className="extAgents-note-t">{t('gui.machine.next_title')}</div>
            <div className="extAgents-note-p">{t('gui.machine.next_body')}</div>
          </div>
        ) : null}
        <Field k="id">{row.id}</Field>
        <Field k={t('gui.machine.f_transport')}>{row.transport === 'local' ? t('gui.machine.transport_local') : t('gui.machine.transport_ssh')}</Field>
        {row.transport === 'ssh' ? (
          <div className="extAgents-fld">
            <span className="extAgents-k">{t('gui.machine.f_auth')}</span>
            <p className="extAgents-ro">{row.auth === 'password' ? t('gui.machine.auth_stored') : t('gui.machine.auth_key')}</p>
            {/* A password machine gets no ssh alias, which is the one thing the
                owner would otherwise expect of a registered machine. */}
            {row.auth === 'password' ? <div className="extAgents-about">{t('gui.machine.auth_note')}</div> : null}
          </div>
        ) : null}
        <Field k={t('gui.machine.f_node')}>
          {busy ? (
            <>
              <Spin /> {row.install.detail || t('gui.machine.installing')}
            </>
          ) : row.transport === 'local' ? (
            t('gui.machine.node_here')
          ) : row.node_current ? (
            row.node_version
          ) : row.node_version ? (
            t('gui.machine.node_stale', { v: row.node_version })
          ) : (
            t('gui.machine.node_bare_none')
          )}
        </Field>
        {needsNode(row) && !busy ? (
          <label className="extAgents-fld">
            <span className="extAgents-k">{t('gui.machine.f_node_dir')}</span>
            <input
              aria-label={t('gui.machine.f_node_dir')}
              className="extAgents-in extAgents-in-mono"
              placeholder="~/.raven-node"
              ref={dirRef}
              type="text"
            />
            <div className="extAgents-about">{t('gui.machine.node_dir_hint')}</div>
          </label>
        ) : null}
        <Field k={t('gui.machine.paths')}>{row.paths.length ? row.paths.join('\n') : t('gui.machine.paths_none')}</Field>
        {row.transport === 'ssh' ? (
          <Field k={t('gui.machine.agents_here')}>
            {agentsHere(row).length ? agentsHere(row).join('\n') : t('gui.machine.agents_here_none')}
          </Field>
        ) : null}
        <Field k={t('gui.machine.problems')}>
          {shownProblems.length ? shownProblems.join('\n') : t('gui.machine.no_problems')}
        </Field>
      </div>
      <div className="extAgents-act">
        {needsNode(row) || busy ? (
          <button className="mini go" disabled={busy || removing} onClick={install}>
            {busy ? <Spin /> : null}
            {busy ? t('gui.machine.installing') : installLabel(row)}
          </button>
        ) : null}
        {row.transport === 'ssh' ? (
          <button className="mini" disabled={probing || removing} onClick={() => void machines.probe(row.id)}>
            {probing ? <Spin /> : null}
            {t('gui.machine.probe')}
          </button>
        ) : null}
        <button className="mini" disabled={removing} onClick={() => void machines.doctor()}>
          {t('gui.machine.doctor')}
        </button>
        <button className="mini danger" disabled={busy || removing} onClick={remove}>
          {removing ? <Spin /> : null}
          {t('gui.machine.remove_do')}
        </button>
        {hosts ? (
          <button className="mini go" disabled={removing} onClick={() => store.machineConnectOpen(row.id)}>
            {t('gui.machine.connect_here')}
          </button>
        ) : null}
      </div>
    </div>,
    dHost
  )
}

/* The form that connects an agent on a machine, opened from the machine's
   sheet: the preset, its working directory there, and Connect -- the same add
   a preset's own sheet makes with "Runs on", from the other side. It lists
   presets the roster withholds (one already connected on this computer is
   still to be had on a machine) and greys the ones this machine has; the name
   is the server's default, `<preset>@<machine id>`, which is also what the
   server holds the machine to -- a second entry of one preset there is refused
   under that name, however the add arrives -- so nothing here collides with
   the row of the same preset that runs here. */
export function MachineConnectSheet({ row, s }: { row: MachineRow; s: ExtAgentsState }): JSX.Element {
  const dHost = store.detailHost()
  const cwdRef = useRef<HTMLInputElement>(null)
  const [picked, setPicked] = useState('')
  /* `taken`: the machine has it (the server would refuse a second). `pending`:
     an add of it on this machine is still running, from this opening or one
     the reader closed -- the server has not written it yet, so `on_machines`
     cannot say, and a second press would be a second ping ending in the
     duplicate refusal. */
  const choices = (s.presets ?? []).map(p => {
    /* `off`: the machine has an entry of it switched off, by that entry's
       name. It still holds the machine (the server refuses a second), so it
       is offered back -- Reconnect switches that entry on -- rather than
       greyed as taken or counted nowhere. */
    const off = p.off_on_machines.find(e => e.machine === row.id)?.name ?? ''
    return {
      ...p,
      off,
      taken: p.on_machines.includes(row.id),
      pending: s.mconnectPending.includes(`${p.preset}@${row.id}`) || (!!off && s.mconnectPending.includes(off))
    }
  })
  const pickable = choices.filter(c => !c.taken && !c.pending)
  const chosen: (typeof choices)[number] | undefined = pickable.find(c => c.preset === picked) ?? pickable[0]
  const busy = s.mconnectBusy !== null && s.mconnectBusy === s.mconnectGen
  const submit = async (): Promise<void> => {
    if (!chosen) return
    const { landed, current, name } = chosen.off
      ? await store.reconnectOnMachine(chosen.off, row.id)
      : await store.connectOnMachine(chosen, row.id, cwdRef.current?.value.trim() || '')
    if (landed) {
      toast(t(chosen.off ? 'gui.machine.reconnected_said' : 'gui.machine.connected_said', { name }))
      /* The card's own line changes with it; back to the sheet that says so --
         if this form is still the one open. The reader may have closed the
         drawer or opened another machine while the ping ran, and what they
         look at now is theirs: the add landed either way, and the toast says. */
      void machines.load()
      if (current) store.machineSheetOpen(row.id)
    } else if (!current) {
      /* The form the refusal belongs to is gone, so one line instead. */
      toast(t('gui.machine.connect_bad_named', { name }))
    }
  }
  const label = (c: (typeof choices)[number]): string =>
    c.taken
      ? t('gui.machine.preset_on_machine', { name: c.name })
      : c.pending
        ? t('gui.machine.preset_connecting', { name: c.name })
        : c.off
          ? t('gui.machine.preset_off_on_machine', { name: c.name })
          : c.connected_here
            ? t('gui.machine.preset_connected_here', { name: c.name })
            : c.name
  return createPortal(
    <div className="extAgents-sheet" aria-label={t('gui.machine.connect_title', { name: row.name })} role="document">
      <div className="extAgents-head">
        <MachineTile />
        <div className="extAgents-meta">
          <h3>{t('gui.machine.connect_title', { name: row.name })}</h3>
          <div className={'extAgents-by' + (s.mconnectFailed && !busy ? ' extAgents-by-bad' : '')}>
            {busy
              ? t('gui.machine.connecting')
              : s.mconnectFailed
                ? t('gui.machine.connect_bad')
                : chosen?.off
                  ? t('gui.machine.reconnect_hint')
                  : t('gui.machine.connect_hint')}
          </div>
        </div>
      </div>
      <div className="extAgents-body">
        {s.presets === null && !s.presetsFailed ? (
          <div className="extAgents-about">{t('gui.machine.presets_loading')}</div>
        ) : s.presetsFailed ? (
          <div className="extAgents-note extAgents-note-warn">
            <div className="extAgents-note-said">{s.presetsFailed}</div>
          </div>
        ) : pickable.length === 0 ? (
          <div className="extAgents-about">{t('gui.machine.presets_none')}</div>
        ) : (
          <>
            <label className="extAgents-fld">
              <span className="extAgents-k">{t('gui.machine.f_preset')}</span>
              <select
                aria-label={t('gui.machine.f_preset')}
                className="extAgents-in"
                onChange={e => setPicked(e.target.value)}
                value={chosen?.preset ?? ''}
              >
                {choices.map(c => (
                  <option disabled={c.taken || c.pending} key={c.preset} value={c.preset}>
                    {label(c)}
                  </option>
                ))}
              </select>
            </label>
            {chosen?.description ? <div className="extAgents-about">{chosen.description}</div> : null}
            {/* A switched-off entry keeps the directory it was connected with;
                only a new entry takes one here. */}
            {chosen?.off ? null : (
              <label className="extAgents-fld">
                <span className="extAgents-k">{t('gui.machine.remote_cwd')}</span>
                <input
                  aria-label={t('gui.machine.remote_cwd')}
                  className="extAgents-in extAgents-in-mono"
                  placeholder="~/raven-work"
                  ref={cwdRef}
                  type="text"
                />
              </label>
            )}
            <div className="extAgents-about">{t('gui.machine.next_body')}</div>
          </>
        )}
        {s.mconnectFailed && !busy ? (
          <div className="extAgents-note extAgents-note-warn">
            <div className="extAgents-note-t">{t('gui.machine.connect_bad')}</div>
            <div className="extAgents-note-said">{s.mconnectFailed}</div>
          </div>
        ) : null}
      </div>
      <div className="extAgents-act">
        <button className="mini go" disabled={busy || !chosen} onClick={() => void submit()} type="button">
          {busy ? <Spin /> : null}
          {t(busy ? 'gui.machine.connecting' : chosen?.off ? 'gui.machine.reconnect_do' : 'gui.machine.connect_do')}
        </button>
      </div>
    </div>,
    dHost,
  )
}

export function RegisterSheet({ ms }: { ms: MachinesState }): JSX.Element {
  const dHost = store.detailHost()
  const idRef = useRef<HTMLInputElement>(null)
  const nameRef = useRef<HTMLInputElement>(null)
  const hostRef = useRef<HTMLInputElement>(null)
  const portRef = useRef<HTMLInputElement>(null)
  const userRef = useRef<HTMLInputElement>(null)
  const keyRef = useRef<HTMLInputElement>(null)
  const passwordRef = useRef<HTMLInputElement>(null)
  const pathsRef = useRef<HTMLTextAreaElement>(null)
  const noteRef = useRef<HTMLDivElement>(null)
  const [ready, setReady] = useState(false)
  /* How Raven logs in, every time: with a key the owner already has, or with
     a password the server keeps on this computer for the owner alone.
     Switching empties the other field with the form: what was typed for one
     way in does not travel with the other. */
  const [auth, setAuth] = useState<RegisterForm['auth']>('key')
  /* The form is longer than the sheet, so a refusal's note under it is
     brought into view; its kind is in the head line anyway, which never
     scrolls away. */
  useEffect(() => {
    const el = noteRef.current
    if (el && ms.addFailed && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' })
  }, [ms.addFailed])
  const read = (): RegisterForm => ({
    id: idRef.current?.value.trim() || '',
    name: nameRef.current?.value.trim() || '',
    host: hostRef.current?.value.trim() || '',
    port: Number(portRef.current?.value.trim() || 0) || 0,
    user: userRef.current?.value.trim() || '',
    auth,
    key: auth === 'key' ? keyRef.current?.value.trim() || '' : '',
    /* Not trimmed: a password is whatever was typed. */
    password: auth === 'password' ? passwordRef.current?.value || '' : '',
    paths: (pathsRef.current?.value || '').split('\n').map((x) => x.trim()).filter(Boolean),
  })
  const check = (): void => {
    const f = read()
    setReady(!!(f.id && f.name && f.host && f.user && (f.auth === 'password' ? f.password : f.key)))
  }
  const pickAuth = (which: RegisterForm['auth']): void => {
    setAuth(which)
    /* The field for the other way in unmounts empty, so the form is not ready
       until something is typed into it. */
    setReady(false)
  }
  /* The key field starts filled with a key this computer has, ssh's own
     first, rather than a placeholder naming one the owner may not have. Read
     once per page; filled whenever the field is drawn empty, which is also
     after a switch back from Password. */
  useEffect(() => {
    if (ms.keys === null) void machines.loadKeys()
  }, [ms.keys])
  useEffect(() => {
    const el = keyRef.current
    const first = ms.keys?.[0]
    if (el && first && !el.value) {
      el.value = first
      check()
    }
  })
  const submit = async (): Promise<void> => {
    const f = read()
    if (await machines.register(f)) store.machineSheetOpen(f.id)
  }
  const text = (ref: React.RefObject<HTMLInputElement | null>, label: string, placeholder = '', mono = false): JSX.Element => (
    <label className="extAgents-fld">
      <span className="extAgents-k">{label}</span>
      <input
        aria-label={label}
        className={mono ? 'extAgents-in extAgents-in-mono' : 'extAgents-in'}
        onChange={check}
        placeholder={placeholder}
        ref={ref}
        type="text"
      />
    </label>
  )
  return createPortal(
    <div className="extAgents-sheet" aria-label={t('gui.machine.register')} role="document">
      <div className="extAgents-head">
        <MachineTile />
        <div className="extAgents-meta">
          <h3>{t('gui.machine.register')}</h3>
          <div className={'extAgents-by' + (ms.addFailed ? ' extAgents-by-bad' : '')}>
            {ms.adding
              ? t('gui.machine.adding')
              : ms.addFailed
                ? ms.addReason
                  ? t(WHY[ms.addReason])
                  : t('gui.machine.add_bad')
                : t('gui.machine.add_hint')}
          </div>
        </div>
      </div>
      <div className="extAgents-body">
        {text(nameRef, t('gui.machine.f_name'), 'GPU box 1')}
        {text(idRef, t('gui.machine.f_id'), 'gpu1', true)}
        {text(hostRef, t('gui.machine.f_host'), '', true)}
        {text(portRef, t('gui.machine.f_port'), '22', true)}
        {text(userRef, t('gui.machine.f_user'), '', true)}
        <div className="extAgents-fld">
          <span className="extAgents-k">{t('gui.machine.f_auth')}</span>
          <span aria-label={t('gui.machine.f_auth')} className="extAgents-seg" role="group">
            <button aria-pressed={auth === 'key'} onClick={() => pickAuth('key')} type="button">
              {t('gui.machine.auth_key')}
            </button>
            <button aria-pressed={auth === 'password'} onClick={() => pickAuth('password')} type="button">
              {t('gui.machine.auth_password')}
            </button>
          </span>
        </div>
        {/* Keyed, so the pick swaps the element rather than patching one
            input's type: unkeyed, React kept the DOM node and what was typed
            as a password went out as a key path (reviewed 2026-10-19). */}
        {auth === 'key' ? (
          <Fragment key="key">
            <label className="extAgents-fld">
              <span className="extAgents-k">{t('gui.machine.f_key')}</span>
              <input
                aria-label={t('gui.machine.f_key')}
                className="extAgents-in extAgents-in-mono"
                list="extAgents-keys"
                onChange={check}
                placeholder={ms.keys && ms.keys.length ? '' : '~/.ssh/id_ed25519'}
                ref={keyRef}
                type="text"
              />
              <datalist id="extAgents-keys">
                {(ms.keys ?? []).map(k => (
                  <option key={k} value={k} />
                ))}
              </datalist>
            </label>
            <div className="extAgents-about">
              {ms.keys === null
                ? ''
                : ms.keys.length
                  ? t('gui.machine.keys_found') + ' '
                  : t('gui.machine.keys_none') + ' '}
              {t('gui.machine.key_note')}
            </div>
          </Fragment>
        ) : (
          <Fragment key="password">
            <label className="extAgents-fld">
              <span className="extAgents-k">{t('gui.machine.f_password')}</span>
              <input
                aria-label={t('gui.machine.f_password')}
                autoComplete="off"
                className="extAgents-in"
                onChange={check}
                ref={passwordRef}
                type="password"
              />
            </label>
            <div className="extAgents-about">{t('gui.machine.password_note')}</div>
          </Fragment>
        )}
        <label className="extAgents-fld">
          <span className="extAgents-k">{t('gui.machine.f_paths')}</span>
          <textarea aria-label={t('gui.machine.f_paths')} onChange={check} placeholder="/data/alice/projects" ref={pathsRef} />
        </label>
        {ms.addFailed ? (
          <div className="extAgents-note extAgents-note-warn" ref={noteRef}>
            <div className="extAgents-note-t">{t('gui.machine.add_bad')}</div>
            <div className="extAgents-note-said">{ms.addFailed}</div>
          </div>
        ) : null}
      </div>
      <div className="extAgents-act">
        <button className="mini go" disabled={!ready || ms.adding} onClick={() => void submit()}>
          {ms.adding ? <Spin /> : null}
          {t(ms.adding ? 'gui.machine.adding' : 'gui.machine.add_do')}
        </button>
      </div>
    </div>,
    dHost,
  )
}

/* Where an agent row will run, picked while it is still a preset: this
   computer, or a registered machine with this Raven's node. A machine without
   one is listed but not pickable -- the add would be refused there -- and the
   option says why. An acp or cli row also takes its working directory on the
   machine; an openai row reaches its endpoint from the machine and has none. */
export function WhereField({ row, ms }: { row: { name: string; kind: string }; ms: MachinesState }): JSX.Element {
  const where = store.whereOf(row.name)
  const pick = (machine: string): void => store.setWhere(row.name, machine, where.remote_cwd)
  const pickable = ms.rows.filter((m) => m.transport === 'ssh')
  return (
    <>
      <label className="extAgents-fld">
        <span className="extAgents-k">{t('gui.machine.where')}</span>
        <select aria-label={t('gui.machine.where')} className="extAgents-in" onChange={(e) => pick(e.target.value)} value={where.machine}>
          <option value="">{t('gui.machine.where_here')}</option>
          {pickable.map((m) => (
            <option disabled={!m.node_current} key={m.id} value={m.id}>
              {m.name} ({m.id}){m.node_current ? '' : ` - ${t(m.node_version ? 'gui.machine.needs_update' : 'gui.machine.needs_node')}`}
            </option>
          ))}
        </select>
        {ms.loaded && pickable.length === 0 ? <div className="extAgents-about">{t('gui.machine.where_none')}</div> : null}
      </label>
      {where.machine && row.kind !== 'openai' ? (
        <label className="extAgents-fld">
          <span className="extAgents-k">{t('gui.machine.remote_cwd')}</span>
          <input
            aria-label={t('gui.machine.remote_cwd')}
            className="extAgents-in extAgents-in-mono"
            defaultValue={where.remote_cwd}
            onChange={(e) => store.setWhere(row.name, where.machine, e.target.value.trim())}
            placeholder="~/raven-work"
            type="text"
          />
        </label>
      ) : null}
    </>
  )
}
