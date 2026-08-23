import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useSyncExternalStore } from 'react'

import { shell, t } from '../../shell/bridge'
import * as store from './store'

import type { XaRow } from './types'
import type { JSX } from 'react'

/* Connect the agents already installed on this machine, so Raven can hand
   work to them. One row per agent; the rows are whatever `DS.xa` answers --
   the fixture source with no gateway behind the page, the `subagents.*`
   source in the live layer. Classes and structure are the legacy page's,
   verbatim: this island changed who owns the data, not a pixel. */

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

/* The one-line health summary. Order matters: the reason it cannot run beats
   the fact that it is switched off, because that is the one the user has to
   act on. */
function stateOf(row: XaRow): { cls: string; text: string } {
  /* A built-in agent is this process. There is nothing to install and nothing
     to reach, so the only fact worth a line is whether it is switched on --
     reading it through the probe verdicts would report "not measured" about a
     loop that is demonstrably running. */
  if (row.builtin) {
    return row.enabled ? { cls: 'ok', text: t('gui.agent.inprocess') } : { cls: 'off', text: t('gui.agent.disabled') }
  }
  if (row.kind === 'openai' && !row.has_api_key) return { cls: 'bad', text: t('gui.agent.needs_key') }
  if (row.configured && !row.enabled && row.probe_status !== 'missing') {
    return { cls: 'off', text: t('gui.agent.disabled') }
  }
  /* Four probe verdicts, and they are not two. `attention` means the binary
     is there but nothing has verified it can do a task -- an amber nudge, not
     a failure. `unknown` is "not measured", which earns no colour at all. */
  if (row.probe_status === 'ready') return { cls: 'ok', text: t('gui.agent.ready') }
  if (row.probe_status === 'attention') return { cls: 'warn', text: row.probe_detail || t('gui.agent.unverified') }
  if (row.probe_status === 'unknown') return { cls: 'off', text: row.probe_detail || '' }
  return { cls: 'bad', text: row.probe_detail || t('gui.agent.missing') }
}

function testLine(row: XaRow): string {
  if (row.test_running) return t('gui.agent.testing')
  if (row.last_test_ok == null) return t('gui.agent.test_never')
  /* fmtStamp is the live layer's, reachable only if it stands on window;
     the standalone demo has no live layer, hence the guard. */
  const f = (window as Window & { fmtStamp?: (ms: number) => string }).fmtStamp
  const ago = !row.last_test_at_ms
    ? ''
    : typeof f === 'function'
      ? f(row.last_test_at_ms)
      : new Date(row.last_test_at_ms).toLocaleString()
  return t(row.last_test_ok ? 'gui.agent.test_ok' : 'gui.agent.test_bad', { ago })
}

const hueOf = (name: string): number => {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return h % 8
}

function confirmUpgrade(row: XaRow): void {
  shell().confirmAsk(
    t('gui.agent.upgrade_do'),
    t('gui.agent.upgrade_body', { name: row.name }),
    t('gui.agent.upgrade_do'),
    () => void store.run('upgrade', row),
  )
}

/* The preset moved to another transport since this entry was written.
   Rewriting it silently would change its command line and invalidate every
   session handle bound to it, so the switch is offered, never applied. */
function UpgradeRow({ row }: { row: XaRow }): JSX.Element {
  return (
    <div className="keyrow">
      <span className="pnote">{t('gui.agent.upgrade', { to: kindText(row.upgrade_to || '') })}</span>
      <button className="mini" onClick={() => confirmUpgrade(row)}>
        {t('gui.agent.upgrade_do')}
      </button>
    </div>
  )
}

/* One package-supplied row: a built-in agent, or one of the vendored builds.
   Deliberately not the configured-agent card: two of that card's three verbs
   mean nothing here (there is no command to test and no row to disconnect), and
   drawing them disabled reads as something being wrong.

   Neither kind gets a switch, for different reasons. A built-in row cannot leave
   the roster at all: an unnamed `spawn` and a dag node with no `subagent` both
   dispatch to it, so `subagents.toggle` refuses the name and the package's seed
   outranks any stored `enabled`. A vendored row is not in config and its name is
   not a built-in one, so the same call answers `subagent_not_found`; its
   membership is the folder's, so the way to take one out is to remove or disable
   the folder (`"enabled": false` in its `subagent.json`), which is what the
   section hint says. Offering a switch that errors is worse than offering none. */
function BuiltinCard({ row, install = false }: { row: XaRow; install?: boolean }): JSX.Element {
  const st = stateOf(row)
  const open = () => store.sheetOpen(row)
  return (
    <div
      className="pcard"
      role="button"
      tabIndex={0}
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === 'Enter') open()
      }}
    >
      <div className="nm">
        <span className={'led' + (st.cls === 'ok' ? '' : ' ' + st.cls)} />
        <span>{row.name}</span>
        <span className="kd">{kindText(row.kind)}</span>
      </div>
      <div className={'mo' + (st.cls === 'ok' || st.cls === 'off' ? '' : ' ' + st.cls)}>{st.text}</div>
      <div className="one">{row.description || ''}</div>
      <div className="ctl">
        {/* Only the state Install fixes, plus the one it is already fixing.
            `!row.enabled` alone also covered a folder its own `subagent.json`
            switched off and one with no credential to spend, and for those the
            button runs for minutes and comes back with the row unchanged -- and
            since a vendored row deliberately has no switch, it would be the only
            affordance offered and a dead end. `row.building` has to be in the
            condition because a row being built reports `attention`, not
            `missing`: dropping it would hide the button mid-build, which is the
            one moment it has something to say. */}
        {install && (row.building || row.probe_status === 'missing') ? (
          <button
            className="mini"
            disabled={!!row.building}
            title={t('gui.agent.install_note')}
            onClick={(e) => {
              e.stopPropagation()
              void store.run('build', row, {})
            }}
          >
            {t(row.building ? 'gui.agent.installing' : 'gui.agent.install')}
          </button>
        ) : null}
        <button
          className="mini ghost"
          onClick={(e) => {
            e.stopPropagation()
            open()
          }}
        >
          {t('gui.agent.configure')}
        </button>
      </div>
    </div>
  )
}

function ConfiguredRow({ row }: { row: XaRow }): JSX.Element {
  const st = stateOf(row)
  const open = () => store.sheetOpen(row)
  return (
    <div
      className="pcard"
      role="button"
      tabIndex={0}
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === 'Enter') open()
      }}
    >
      <div className="nm">
        <span className={'led' + (st.cls === 'ok' ? '' : ' ' + st.cls)} />
        <span>{row.name}</span>
        <span className="kd">{kindText(row.kind)}</span>
      </div>
      <div className={'mo' + (st.cls === 'ok' || st.cls === 'off' ? '' : ' ' + st.cls)}>
        {[st.text, testLine(row)].filter(Boolean).join(' · ')}
      </div>
      {/* What the verdict actually proved, skipped when the status line above
          already says the same sentence: a failed handshake used to print
          itself twice, once red and once grey. */}
      {row.last_test_detail && !String(st.text || '').includes(row.last_test_detail) ? (
        <div className="pnote clamp" onClick={(e) => e.currentTarget.classList.toggle('clamp')}>
          {row.last_test_detail}
        </div>
      ) : null}
      {row.upgrade_to ? <UpgradeRow row={row} /> : null}
      {/* Two verbs on the row, everything else in the detail sheet: the list
          answers "which agents, are they alive". The whole row is a door to
          the same place. */}
      <div className="ctl">
        {row.test_running ? (
          <button
            className="mini ghost"
            onClick={(e) => {
              e.stopPropagation()
              void store.run('test_cancel', row)
            }}
          >
            {t('gui.agent.test_cancel')}
          </button>
        ) : (
          <button
            className="mini ghost"
            data-tip={t('gui.agent.test_spend', { name: row.name })}
            onClick={(e) => {
              e.stopPropagation()
              void store.run('test', row)
            }}
          >
            {t('gui.agent.test')}
          </button>
        )}
        <button
          className="mini ghost"
          onClick={(e) => {
            e.stopPropagation()
            open()
          }}
        >
          {t('gui.agent.configure')}
        </button>
      </div>
    </div>
  )
}

function OffCard({ row }: { row: XaRow }): JSX.Element {
  const st = stateOf(row)
  return (
    <div className="card" onClick={() => store.sheetOpen(row)}>
      <span className={'pmtile th' + hueOf(row.name)}>{(row.name[0] || '?').toUpperCase()}</span>
      <div className="nm">
        <span>{row.name}</span>
        <span className="kd">{kindText(row.kind)}</span>
      </div>
      <div className="one">{row.description || ''}</div>
      {st.text ? <div className={'st' + (st.cls === 'ok' || st.cls === 'off' ? '' : ' ' + st.cls)}>{st.text}</div> : null}
      <div className="ctl">
        <button
          className="mini"
          onClick={(e) => {
            e.stopPropagation()
            /* An HTTP agent cannot answer without its key, so connecting it
               opens the sheet at the form rather than a one-click add: the
               alternative lands a disabled row and leaves the user hunting
               for why. */
            if (row.kind === 'openai') {
              store.sheetOpen(row)
              return
            }
            void store.run('connect', row, {})
          }}
        >
          {t('gui.agent.connect')}
        </button>
      </div>
    </div>
  )
}

/* The agent's detail sheet: everything about one row in one place, drawn
   into the same #detail dialog the plugin and skill details use -- identity
   block up top, one decisive action beside it, then sections. Editing
   happens HERE, never squeezed into the list row.

   Configure means name, description, and -- only where one is needed -- the
   api key. Never the command line. That comes from the preset, which is
   version verified; letting the page edit it is how a working agent turns
   into a command nobody can account for. */
function XaSheet({ row }: { row: XaRow }): JSX.Element {
  const dHost = store.detailHost()
  useEffect(() => {
    const title = document.getElementById('dTitle')
    if (title) title.textContent = ''
    const drawer = document.getElementById('detail')
    if (drawer) drawer.dataset.open = 'true'
  }, [row])
  const st = stateOf(row)
  /* Rows the package supplies: a built-in agent, or one of the vendored builds.
     Neither `connect` nor `update` can act on one -- `subagents.add` wants a
     preset name and answers `unknown preset: 'raven'` / `'Raven-PPT'`, and
     `subagents.update` wants a config row to edit. Both buttons were offered
     anyway, the built-in one since before this change. */
  const packaged = !!row.builtin || !!row.vendored
  const nameRef = useRef<HTMLInputElement>(null)
  const descRef = useRef<HTMLTextAreaElement>(null)
  const keyRef = useRef<HTMLInputElement>(null)
  const save = () => {
    const patch = {
      new_name: nameRef.current ? nameRef.current.value.trim() : '',
      description: descRef.current ? descRef.current.value : '',
      api_key: keyRef.current ? keyRef.current.value : '',
    }
    void store.run(row.configured ? 'update' : 'connect', row, patch)
  }
  return createPortal(
    <>
      <div className="pmdhead">
        <span className={'pmtile th' + hueOf(row.name)}>{(row.name[0] || '?').toUpperCase()}</span>
        <div className="pmdmeta">
          <div className="l1">
            <span className={'led' + (st.cls === 'ok' ? '' : ' ' + st.cls)} />
            <b>{row.name}</b>
            <span className="kd">{kindText(row.kind)}</span>
          </div>
          <div className="l2">{row.description || ''}</div>
        </div>
        <div className="dact">
          {row.vendored ? (
            /* Install is the one action a vendored row has, and only for the one
               state it fixes: a folder whose dependencies are missing. A row
               disabled because its manifest says so, or because nothing can pay
               for its model, is not something Install can help -- it would run
               for minutes and come back unchanged. */
            row.building || row.probe_status === 'missing' ? (
              <button
                className="mini"
                disabled={!!row.building}
                title={t('gui.agent.install_note')}
                onClick={() => void store.run('build', row, {})}
              >
                {t(row.building ? 'gui.agent.installing' : 'gui.agent.install')}
              </button>
            ) : null
          ) : row.builtin ? (
            /* No head action at all. No connect (already running), no test
               (nothing to spend), no disconnect (not writing a row is what "use
               the default" means) -- and no switch either: an unnamed `spawn` and
               a dag node with no `subagent` both dispatch to this row, so
               `subagents.toggle` refuses the name and the package's seed outranks
               any stored `enabled`. The sheet is where its description is read. */
            null
          ) : !row.configured ? (
            /* Connecting an HTTP agent needs its key first, so the head
               action defers to the form's save; every other kind connects
               in one click. */
            row.kind !== 'openai' ? (
              <button className="mini" onClick={() => void store.run('connect', row, {})}>
                {t('gui.agent.connect')}
              </button>
            ) : null
          ) : row.test_running ? (
            <button className="mini ghost" onClick={() => void store.run('test_cancel', row)}>
              {t('gui.agent.test_cancel')}
            </button>
          ) : (
            <button
              className="mini"
              data-tip={t('gui.agent.test_spend', { name: row.name })}
              onClick={() => void store.run('test', row)}
            >
              {t('gui.agent.test')}
            </button>
          )}
        </div>
      </div>
      <div className="pmsec">
        <span className="cap">{t('gui.agent.sec_status')}</span>
        <div
          className={'probe' + (st.cls === 'ok' ? ' ok' : st.cls === 'bad' ? ' bad' : '')}
          style={{ marginTop: 0 }}
        >
          {row.test_running ? t('gui.agent.testing') : st.text || '—'}
        </div>
        <dl className="kv" style={{ marginTop: '10px' }}>
          <dt>{t('gui.agent.sec_transport')}</dt>
          <dd>{kindText(row.kind)}</dd>
          <dt>{t('gui.agent.sec_last_test')}</dt>
          <dd>{testLine(row)}</dd>
        </dl>
        {row.last_test_detail && !String(st.text || '').includes(row.last_test_detail) ? (
          <div className="pnote">{row.last_test_detail}</div>
        ) : null}
        {row.upgrade_to ? <UpgradeRow row={row} /> : null}
      </div>
      <div className="pmsec">
        <span className="cap">{t('gui.agent.sec_config')}</span>
        <div className="pform">
          <div className="agf">
            <label>{t('gui.agent.name')}</label>
            <input type="text" defaultValue={row.name} ref={nameRef} />
            <div className="hint">{t('gui.agent.name_hint')}</div>
          </div>
          <div className="agf">
            <label>{t('gui.agent.desc')}</label>
            <textarea rows={2} defaultValue={row.description || ''} ref={descRef} />
            <div className="hint">{t('gui.agent.desc_hint')}</div>
          </div>
          {row.kind === 'openai' ? (
            <div className="agf">
              <label>{t('gui.agent.key')}</label>
              <input
                type="password"
                autoComplete="off"
                placeholder={row.has_api_key ? t('gui.agent.key_set') : ''}
                ref={keyRef}
              />
              <div className="hint">{t('gui.agent.key_hint')}</div>
            </div>
          ) : null}
          <div className="ctl">
            {packaged ? (
              <div className="hint">{t('gui.agent.packaged_note')}</div>
            ) : (
              <button className="mini" onClick={save}>
                {t(row.configured ? 'gui.agent.save' : 'gui.agent.connect')}
              </button>
            )}
          </div>
        </div>
      </div>
      {row.configured ? (
        <div className="pmsec">
          <div className="ctl" style={{ justifyContent: 'flex-start' }}>
            <button
              className="mini ghost"
              onClick={() => void store.run('toggle', row, { enabled: !row.enabled })}
            >
              {t(row.enabled ? 'gui.agent.disable' : 'gui.agent.enable')}
            </button>
            <button
              className="mini ghost danger"
              onClick={() =>
                shell().confirmAsk(
                  t('gui.agent.disconnect'),
                  t('gui.agent.disc_body', { name: row.name }),
                  t('gui.agent.disconnect'),
                  () => {
                    store.closeSheet()
                    void store.run('remove', row)
                  },
                )
              }
            >
              {t('gui.agent.disconnect')}
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
  const builtin = s.rows.filter((a) => a.builtin)
  /* Vendored rows are their own group, and they have to come out of `off`:
     that section's verb is "connect", and there is nothing to connect. They are
     folders this install shipped, discovered on every start, so what a row there
     actually offers is the same set a built-in row does -- a switch and a look
     inside. A row a user has additionally written by hand arrives as configured
     rather than vendored, and stays in `on` with its own delete button. */
  const vendored = s.rows.filter((a) => a.vendored && !a.builtin)
  const on = s.rows.filter((a) => a.configured && !a.builtin && !a.vendored)
  const off = s.rows.filter((a) => !a.configured && !a.builtin && !a.vendored)
  const sheetRow = s.sheet ? s.rows.find((x) => x.name === s.sheet) : undefined
  return (
    <>
      <div className="pmhero">
        <h3>{t('gui.agent.hero')}</h3>
        <p>{t('gui.agent.hero_sub')}</p>
      </div>
      {/* Built-in first: they are the agents a fresh install already has, so a
          page that led with "nothing connected yet" would be describing the
          roster wrongly. */}
      {builtin.length ? (
        <div className="csec">
          <div className="hd">
            <b>{t('gui.agent.builtin')}</b>
            <span className="n">{String(builtin.length)}</span>
          </div>
          <div className="sec-hint">{t('gui.agent.builtin_h')}</div>
          <div className="fset">
            {builtin.map((row) => (
              <BuiltinCard key={row.name} row={row} />
            ))}
          </div>
        </div>
      ) : null}
      {vendored.length ? (
        <div className="csec">
          <div className="hd">
            <b>{t('gui.agent.vendored')}</b>
            <span className="n">{String(vendored.length)}</span>
          </div>
          <div className="sec-hint">{t('gui.agent.vendored_h')}</div>
          <div className="fset">
            {vendored.map((row) => (
              <BuiltinCard key={row.name} row={row} install />
            ))}
          </div>
        </div>
      ) : null}
      <div className="csec">
        <div className="hd">
          <b>{t('gui.agent.on')}</b>
          <span className="n">{String(on.length)}</span>
          <button className="mini ghost" disabled={s.probing} onClick={() => void store.run('probe')}>
            {t(s.probing ? 'gui.agent.probing' : 'gui.agent.probe')}
          </button>
        </div>
        {!on.length ? (
          <div className="empty-note">{t('gui.agent.none')}</div>
        ) : (
          <div className="fset">
            {on.map((row) => (
              <ConfiguredRow key={row.name} row={row} />
            ))}
          </div>
        )}
      </div>
      <div className="csec">
        <div className="hd">
          <b>{t('gui.agent.off')}</b>
          <span className="n">{String(off.length)}</span>
        </div>
        <div className="grid">
          {off.map((row) => (
            <OffCard key={row.name} row={row} />
          ))}
        </div>
      </div>
      {sheetRow ? <XaSheet key={`${s.sheet}:${s.epoch}`} row={sheetRow} /> : null}
    </>
  )
}
