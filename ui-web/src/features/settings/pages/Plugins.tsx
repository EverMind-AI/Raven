/* Plugins: the MCP servers, each with its switch (plug.toggle), a chip for
   what the connection manager reports, and a panel for its credential. */
import { useEffect, useState } from 'react'

import { KeyInput } from '../../../components/KeyInput'
import { t } from '../../../i18n/t'
import { Card, Chip, KeyLink, Row, Rov, Spin, Switch, Xrow } from '../Fields'
import { KeyFieldsWait } from '../Skeletons'
import * as store from '../store'

import type { McpSnapshot } from '../../../rpc/generated'
import type { AuthField } from '../types'
import type { JSX } from 'react'

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
  return (
    <Chip state="warn" onClick={() => void store.run(busyKey(m.name), () => store.source().retryServer(m.name))}>
      {t('gui.settings.plugins.failed_retry')}
    </Chip>
  )
}

function OauthPanel({ m }: { m: McpSnapshot }): JSX.Element {
  const auth = (): void => { void store.run(busyKey(m.name), () => store.source().authServer(m.name)) }
  const revoke = (): void => { void store.run(busyKey(m.name), () => store.source().revokeServer(m.name)) }
  return (
    <Row label={t('gui.settings.plugins.auth')}>
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

/* The catalog names the credential field; a server nobody installed from the
   catalog has none, and its row has no panel (the list decides that). */
function KeyPanel({ m }: { m: McpSnapshot }): JSX.Element {
  const [fields, setFields] = useState<AuthField[] | null>(null)
  useEffect(() => {
    let live = true
    store.source().serverAuthFields(m.name)
      .then((f) => { if (live) setFields(f) })
      .catch(() => { if (live) setFields([]) })
    return () => { live = false }
  }, [m.name])
  if (fields === null) return <KeyFieldsWait />
  if (!fields.length) return <Row><Rov>{t('gui.settings.plugins.no_credential')}</Rov></Row>
  return (
    <>
      {fields.map((f) => (
        <KeyField key={f.key} m={m} field={f} />
      ))}
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
      <div className="settings-crumb">
        <Rov>{t('gui.settings.plugins.counter', { on: connected, total: rows.length })}</Rov>
      </div>
      <Card>
        {!rows.length && <Row><Rov>{t('gui.settings.plugins.none')}</Rov></Row>}
        {rows.map((m) => {
          const panel = m.auth === 'oauth' ? <OauthPanel m={m} /> : m.auth === 'apikey' ? <KeyPanel m={m} /> : undefined
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
