/* Plugins: the MCP servers, each with its switch (plug.toggle), a chip for
   what the connection manager reports, and a panel for its credential. */
import { useEffect, useState } from 'react'

import { KeyInput } from '../../../components/KeyInput'
import { t } from '../../../i18n/t'
import { Card, Chip, Empty, GLYPH, KeyLink, Row, Rov, Spin, Switch, Xrow } from '../Fields'
import { KeyFieldsWait } from '../Skeletons'
import * as store from '../store'

import type { McpSnapshot } from '../../../rpc/generated'
import type { AuthField, McpDetail } from '../types'
import type { JSX, ReactNode } from 'react'

type PlugChip = 'none' | 'connected' | 'connecting' | 'setup' | 'failed'

/* What the chip says. Off shows nothing; a toggle or retry in flight is
   connecting; a credential the server still lacks is "needs setup" whatever
   the manager says, because nothing will connect until it is saved. */
export function plugChip(m: McpSnapshot, busy: boolean): PlugChip {
  if (!m.enabled) return 'none'
  if (busy || m.state === 'connecting') return 'connecting'
  if (m.auth && m.auth !== 'none' && !m.credentialed) return 'setup'
  if (m.state === 'connected') return 'connected'
  if (m.state === 'error' || m.state === 'auth_required') return 'failed'
  return 'none'
}

const busyKey = (name: string): string => `plug:${name}`

function ChipFor({ m }: { m: McpSnapshot }): JSX.Element | null {
  const kind = plugChip(m, store.isBusy(busyKey(m.name)))
  if (kind === 'none') return null
  if (kind === 'connected') return <Chip state="on">{t('gui.settings.plugins.connected')}</Chip>
  if (kind === 'connecting') return <Spin>{t('gui.settings.plugins.connecting')}</Spin>
  if (kind === 'setup') return <Chip state="warn">{t('gui.settings.plugins.setup')}</Chip>
  /* A status, not a control. "Failed · retry" made the chip a button, so the
     one place that says what state the server is in was also a place a stray
     click reconnected it, beside a switch that already means on and off. The
     retry is in the drawer, beside the reason it failed. */
  return <Chip state="warn">{t('gui.settings.plugins.failed')}</Chip>
}

/* What went wrong and the one thing to try, together and first: for a server
   that will not connect, that is what the reader opened the row for. The
   reason is the manager's own words; a failure that came back without any
   still gets the button. */
function Failure({ m }: { m: McpSnapshot }): JSX.Element | null {
  const busy = store.isBusy(busyKey(m.name))
  if (plugChip(m, busy) !== 'failed' && !(busy && m.error)) return null
  return (
    <div className="settings-pfail">
      {m.error ? <span className="settings-pbad">{m.error}</span> : <span />}
      <button type="button" className="mini" disabled={busy}
        onClick={() => void store.run(busyKey(m.name), () => store.source().retryServer(m.name))}>
        {t(busy ? 'gui.settings.plugins.connecting' : 'gui.settings.plugins.retry')}
      </button>
    </div>
  )
}

/* A sentence rather than a labelled value: an empty label cell is what tells
   the sheet to let the line start at the left, where prose is read. */
function Note({ children, bad }: { children: ReactNode; bad?: boolean }): JSX.Element {
  return <Row><span className={bad ? 'settings-pnote settings-pbad' : 'settings-pnote'}>{children}</span></Row>
}

function OauthRow({ m }: { m: McpSnapshot }): JSX.Element {
  const auth = (): void => { void store.run(busyKey(m.name), () => store.source().authServer(m.name)) }
  const revoke = (): void => { void store.run(busyKey(m.name), () => store.source().revokeServer(m.name)) }
  /* No label: every control here names the thing it does, and an "Authorization"
     label opposite them only bought a panel's width of empty space between the
     word and the button that acts on it. */
  return (
    <Row>
      <span className="settings-taglist">
        {m.credentialed ? (
          <>
            <Rov>{t('gui.settings.plugins.authorized')}</Rov>
            <button type="button" className="mini ghost" onClick={auth}>{t('gui.settings.plugins.reauth')}</button>
            <button type="button" className="mini ghost" onClick={revoke}>{t('gui.settings.plugins.revoke')}</button>
          </>
        ) : (
          <button type="button" className="mini" onClick={auth}>{t('gui.settings.plugins.auth_browser')}</button>
        )}
      </span>
    </Row>
  )
}

/* What the credential part of a drawer says, which depends on two sources that
   can disagree. The runtime row knows WHETHER this server authenticates; the
   catalogue knows WHICH fields it takes. An empty field list used to print
   "takes no credential" whatever the row said, so a server the catalogue has
   no entry for -- anything added by hand, and github, whose entry was filed
   under another id -- was told it needed nothing while its connection failed
   on an expired token. */
function Credential({ m, detail }: { m: McpSnapshot; detail: McpDetail }): JSX.Element {
  if (m.auth === 'oauth') return <OauthRow m={m} />
  if (m.auth === 'none') return <Note>{t('gui.settings.plugins.no_credential')}</Note>
  if (detail.fields.length) {
    return <>{detail.fields.map((f) => <KeyField key={f.key} m={m} field={f} />)}</>
  }
  return <Note>{t(detail.known ? 'gui.settings.plugins.no_credential' : 'gui.settings.plugins.no_fields')}</Note>
}

/* The drawer, in the order a reader needs it: what to do about it, then what
   it is, then what went wrong. Everything here is a fact one of the two
   sources already carried and the panel used to drop -- the address it
   connects to, the tools it brings, and the manager's own error text, which
   only ever reached the reader as the word "failed" on a chip. */
function Panel({ m }: { m: McpSnapshot }): JSX.Element {
  const [detail, setDetail] = useState<McpDetail | null>(null)
  useEffect(() => {
    let live = true
    store.source().serverDetail(m.name)
      .then((d) => { if (live) setDetail(d) })
      .catch(() => { if (live) setDetail({ known: false, fields: [], tools: [] }) })
    return () => { live = false }
  }, [m.name])
  if (!detail) return <KeyFieldsWait />
  return (
    <>
      <Failure m={m} />
      <Credential m={m} detail={detail} />
      {/* Facts, not controls, so they read as a definition list rather than
          through the row grid: a label column the width of its longest word
          and the value right beside it. Sent through the grid they sat at
          opposite edges of the panel, a two-character label a thousand pixels
          from the URL it names, with nothing in between to pair them. */}
      <dl className="settings-pfacts">
        {detail.address && (
          <>
            <dt>{t('gui.settings.plugins.address')}</dt>
            <dd className="settings-pmono" title={detail.address}>{detail.address}</dd>
          </>
        )}
        <dt>{t('gui.settings.plugins.tools')}</dt>
        <dd>
          {t('gui.settings.plugins.tools_n', { n: m.tool_count })}
          {detail.tools.length > 0 && <span className="settings-ptnames">{detail.tools.join('  ')}</span>}
        </dd>
      </dl>
    </>
  )
}

function KeyField({ m, field }: { m: McpSnapshot; field: AuthField }): JSX.Element {
  const [value, setValue] = useState('')
  const save = (): void => {
    const v = value.trim()
    if (!v) { store.refuse(t('gui.settings.plugins.key_first')); return }
    void store.run(busyKey(m.name), () => store.source().configureServer(m.name, { [field.key]: v })).then((ok) => { if (ok) setValue('') })
  }
  const clear = (): void => { void store.run(busyKey(m.name), () => store.source().configureServer(m.name, { [field.key]: '' })) }
  return (
    <Row stack label={<>{field.label || t('gui.settings.plugins.api_key')}<KeyLink url={field.help_url} /></>}>
      <span className="settings-taglist">
        <KeyInput className="settings-tbox" value={value} aria-label={field.label || t('gui.settings.plugins.api_key')}
          placeholder={m.credentialed ? t('gui.settings.key_set_ph') : t('gui.settings.key_ph')}
          onChange={(e) => setValue(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
        <button type="button" className="mini" onClick={save}>{m.credentialed ? t('gui.settings.update') : t('gui.save')}</button>
        {m.credentialed && <button type="button" className="mini ghost" onClick={clear}>{t('gui.settings.clear')}</button>}
      </span>
    </Row>
  )
}

export function Plugins(): JSX.Element {
  const s = store.get()
  const rows = s.snap.mcp
  const connected = rows.filter((m) => plugChip(m, store.isBusy(busyKey(m.name))) === 'connected').length
  const flip = (m: McpSnapshot, on: boolean): void => {
    /* A server turned on that still needs its credential is written as
       enabled and opened, and nothing tries to connect until the key lands. */
    if (on && m.auth && m.auth !== 'none' && !m.credentialed) store.set({ plugOpen: m.name })
    void store.run(busyKey(m.name), () => store.source().toggleServer(m.name, on))
  }
  return (
    <>
      {/* "0 of 0 connected" is a number about nothing: the empty card below
          already says there is no server, so the count only runs with rows. */}
      {rows.length > 0 && (
        <div className="settings-crumb">
          <Rov>{t('gui.settings.plugins.counter', { on: connected, total: rows.length })}</Rov>
        </div>
      )}
      <Card>
        {!rows.length && (
          <Empty icon={GLYPH.plug} title={t('gui.settings.plugins.none')}>{t('gui.settings.plugins.none_sub')}</Empty>
        )}
        {rows.map((m) => {
          const panel = <Panel m={m} />
          return (
            <Xrow
              key={m.name}
              name={<span className="mono">{m.name}</span>}
              status={<ChipFor m={m} />}
              ctl={<Switch on={m.enabled} label={m.name} onChange={(v) => flip(m, v)} />}
              panel={panel}
              open={s.plugOpen === m.name}
              dim={!m.enabled}
              onToggle={() => store.set({ plugOpen: s.plugOpen === m.name ? null : m.name })}
            />
          )
        })}
      </Card>
    </>
  )
}
