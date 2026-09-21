/* The provider list and the inline "add a provider" block. Connected
   providers only; adding one picks a vendor and connects it in one card. */
import { useEffect, useState } from 'react'

import { KeyInput } from '../../../components/KeyInput'
import { t } from '../../../i18n/t'
import { Card, Chip, KeyLink, Row, Rov, Tag } from '../Fields'
import * as store from '../store'

import type { ProvFilter } from '../store'
import type { ProviderRow } from '../types'
import type { JSX } from 'react'

export const AZURE = 'azure_openai'

/* The registry's auth shapes, in the order the picker groups them. */
const KINDS: Array<[string, string]> = [
  ['key', 'gui.settings.providers.kind_key'],
  ['oauth', 'gui.settings.providers.kind_oauth'],
  ['local', 'gui.settings.providers.kind_local'],
]
export const kindOf = (p: ProviderRow): string => (p.kind === 'oauth' ? 'oauth' : p.kind === 'local' || p.kind === 'endpoint' ? 'local' : 'key')
export const kindLabel = (p: ProviderRow): string => t((KINDS.find(([k]) => k === kindOf(p)) || KINDS[0]!)[1])
/* The add block's vendor groups are the catalogue page's four filter buckets,
   answered by one function so the two cannot drift: an aggregator first, since
   it resells whatever shape it takes, then the sign-in and self-hosted shapes,
   and everything else is a vendor held directly -- an endpoint credential too. */
export type ProvGroup = Exclude<ProvFilter, 'all' | 'on'>
const GROUPS: Array<[ProvGroup, string]> = [
  ['direct', 'gui.settings.providers.filter_direct'],
  ['gateway', 'gui.settings.providers.filter_gateway'],
  ['oauth', 'gui.settings.providers.filter_oauth'],
  ['local', 'gui.model.kind.local'],
]
export const groupOf = (p: ProviderRow): ProvGroup =>
  p.gateway ? 'gateway' : p.kind === 'oauth' ? 'oauth' : p.kind === 'local' ? 'local' : 'direct'
/* Whether the pane draws a key field and an address field for this vendor,
   and whether the key is what connects it: a local server may sit behind a
   token but is reached by its address, so its key is optional. */
export const takesKey = (p: ProviderRow): boolean => kindOf(p) !== 'oauth' && p.acceptsKey !== false
export const needsKey = (p: ProviderRow): boolean => kindOf(p) === 'key'
export const takesBase = (p: ProviderRow): boolean => kindOf(p) === 'local' || !!p.needsBase

/* The device-flow code, shown until the provider turns connected. */
export function OauthNote({ slug }: { slug: string }): JSX.Element | null {
  const o = store.get().oauth
  if (!o || o.slug !== slug) return null
  if (o.expired) return <Rov warn>{t('gui.settings.providers.oauth_expired')}</Rov>
  return (
    <span className="settings-rov">
      {t('gui.settings.providers.oauth_code', { code: o.code })}{' '}
      <a href={o.uri} target="_blank" rel="noopener">{o.uri}</a>
    </span>
  )
}

function AddBlock({ slug, hideCancel }: { slug: string; hideCancel?: boolean }): JSX.Element {
  const s = store.get()
  const rows = s.snap.providers.filter((p) => !p.on)
  const p = rows.find((x) => x.id === slug) || rows[0]
  const [key, setKey] = useState('')
  const [base, setBase] = useState(p ? (p.apiBase || p.defaultApiBase || '') : '')
  const [deploy, setDeploy] = useState('')
  const [ver, setVer] = useState('2024-10-21')
  const close = (): void => store.set({ provAdd: null, err: '' })
  if (!p) return <div className="settings-cfg settings-padd"><Row><Rov>{t('gui.settings.providers.all_connected')}</Rov></Row></div>
  const connect = (): void => {
    const k = key.trim()
    const b = base.trim()
    if (needsKey(p) && !k) { store.refuse(t('gui.settings.providers.key_first')); return }
    if (takesBase(p) && !b) { store.refuse(t('gui.settings.providers.base_first')); return }
    void store.run(`connect:${p.id}`, async () => {
      const params: Record<string, unknown> = { slug: p.id }
      if (k) params.api_key = k
      if (b) params.api_base = b
      const snap = await store.source().provider('save_key', params)
      if (p.id === AZURE && (deploy.trim() || ver.trim())) {
        return store.source().setFields(p.id, { deployment: deploy.trim(), api_version: ver.trim() })
      }
      return snap
    }).then((ok) => { if (ok) close() })
  }
  return (
    <div className="settings-cfg settings-padd">
      <Row label={t('gui.settings.providers.vendor')}>
        <span className="settings-selw">
          <select className="settings-sel" value={p.id} aria-label={t('gui.settings.providers.vendor')}
            onChange={(e) => { store.set({ provAdd: e.currentTarget.value }); setKey(''); const n = rows.find((x) => x.id === e.currentTarget.value); setBase(n ? (n.apiBase || n.defaultApiBase || '') : '') }}>
            {GROUPS.map(([kind, label]) => {
              const group = rows.filter((x) => groupOf(x) === kind)
              return group.length ? (
                <optgroup key={kind} label={t(label)}>
                  {group.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
                </optgroup>
              ) : null
            })}
          </select>
        </span>
      </Row>
      {kindOf(p) === 'oauth' ? (
        <Row label={t('gui.settings.providers.auth')}>
          <span className="settings-taglist">
            <button type="button" className="mini" onClick={() => void store.oauthStart(p.id)}>{t('gui.settings.providers.auth_browser')}</button>
            <Tag>{t('gui.settings.providers.subscription')}</Tag>
            <OauthNote slug={p.id} />
          </span>
        </Row>
      ) : (
        <>
          {takesKey(p) && (
            <Row label={<>{t('gui.settings.providers.api_key')}<KeyLink url={p.keyUrl} /></>}>
              <KeyInput className="settings-tbox" value={key} placeholder={t(needsKey(p) ? 'gui.settings.providers.paste_key' : 'gui.settings.providers.key_optional_ph')} aria-label={t('gui.settings.providers.api_key')}
                onChange={(e) => setKey(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter') connect() }} />
            </Row>
          )}
          {(takesBase(p) || p.gateway || p.kind === 'endpoint') && (
            <Row label={t('gui.settings.providers.base')}>
              <input className="settings-tbox" value={base} aria-label={t('gui.settings.providers.base')}
                placeholder={kindOf(p) === 'local' ? 'http://localhost:11434' : (p.needsBase ? 'https://' : (p.defaultApiBase || t('gui.settings.providers.base_default')))}
                onChange={(e) => setBase(e.currentTarget.value)} />
            </Row>
          )}
          {p.id === AZURE && (
            <>
              <Row label={t('gui.settings.providers.deployment')}>
                <input className="settings-tbox" value={deploy} aria-label={t('gui.settings.providers.deployment')}
                  placeholder={t('gui.settings.providers.deployment_ph')} onChange={(e) => setDeploy(e.currentTarget.value)} />
              </Row>
              <Row label={t('gui.settings.providers.api_version')}>
                <input className="settings-tbox" value={ver} aria-label={t('gui.settings.providers.api_version')} onChange={(e) => setVer(e.currentTarget.value)} />
              </Row>
            </>
          )}
        </>
      )}
      <Row>
        <span className="settings-taglist">
          {!hideCancel && <button type="button" className="mini ghost" onClick={close}>{t('gui.cancel')}</button>}
          {kindOf(p) !== 'oauth' && (
            <button type="button" className="mini" disabled={store.isBusy(`connect:${p.id}`)} onClick={connect}>
              {t('gui.settings.providers.connect')}
            </button>
          )}
        </span>
      </Row>
    </div>
  )
}

/* `setup` is the onboarding wizard's step body: no detail page to fall into
   (the manage button becomes disconnect), and with nothing connected yet the
   add form opens on its own rather than waiting for a click on a button that
   would otherwise be the whole page's content. */
export function Providers({ setup }: { setup?: boolean }): JSX.Element {
  const s = store.get()
  const on = s.snap.providers.filter((p) => p.on)
  const off = s.snap.providers.filter((p) => !p.on)
  const openAdd = (): void => store.set({ provAdd: off[0] ? off[0].id : '' })
  const firstOff = off[0] ? off[0].id : ''
  useEffect(() => {
    if (setup && !on.length && s.provAdd === null && firstOff) store.set({ provAdd: firstOff })
  }, [setup, on.length, s.provAdd, firstOff])
  return (
    <Card
      title={t('gui.settings.providers.title', { n: on.length })}
      raw
      act={s.provAdd === null && off.length ? (
        <button type="button" className="mini ghost" onClick={openAdd}>{t('gui.settings.providers.add')}</button>
      ) : undefined}
    >
      {s.provAdd !== null && <AddBlock slug={s.provAdd} hideCancel={setup && !on.length} />}
      {!on.length && s.provAdd === null && <div className="settings-rows"><Row><Rov>{t('gui.settings.providers.none')}</Rov></Row></div>}
      {on.map((p) => (
        <div key={p.id} className="settings-prow2">
          <Chip state="on">{t('gui.settings.providers.connected')}</Chip>
          <span className="settings-pn2">{p.name}</span><span className="settings-kk">{p.id}</span>
          {kindOf(p) === 'oauth' && <Tag>{t('gui.settings.providers.subscription')}</Tag>}
          {(p.configured || []).length > 0 && <span className="settings-kk">{t('gui.settings.providers.n_models', { n: (p.configured || []).length })}</span>}
          <span style={{ flex: 1 }} />
          {setup ? (
            <button type="button" className="mini ghost" disabled={store.isBusy(`disconnect:${p.id}`)}
              onClick={() => void store.run(`disconnect:${p.id}`, () => store.source().provider('disconnect', { slug: p.id }))}>
              {t('gui.settings.providers.disconnect')}
            </button>
          ) : (
            <button type="button" className="mini ghost" onClick={() => store.set({ provider: p.id, err: '' })}>{t('gui.settings.providers.manage')}</button>
          )}
        </div>
      ))}
    </Card>
  )
}
