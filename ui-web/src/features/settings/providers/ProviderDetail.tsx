/* One provider: its connection, the models it lists (with the vendor's own
   list to add from), and the advanced card -- address, headers, display
   names. The refusals are the page's: a provider or model a role uses stays. */
import { useState } from 'react'

import { KeyInput } from '../../../components/KeyInput'
import { ProviderIcon } from '../../../components/ProviderMark'
import { t } from '../../../i18n/t'
import { AddModelPop } from './AddModelPop'
import { Card, IconBtn, KeyLink, Row, Rov, Tag } from '../Fields'
import * as store from '../store'
import { AZURE, OauthNote, kindLabel, kindOf, needsKey, takesBase, takesKey } from './Providers'
import { roleName, rolesUsing } from './Roles'

import type { ProviderRow } from '../types'
import type { JSX } from 'react'

const busy = (slug: string): string => `prov:${slug}`

function Connection({ p }: { p: ProviderRow }): JSX.Element {
  const [key, setKey] = useState('')
  const [base, setBase] = useState(p.apiBase || rawStr(store.get().snap.raw, p.id, 'apiBase') || p.defaultApiBase || '')
  const kind = kindOf(p)
  const disconnect = (): void => {
    const used = rolesUsing(store.get().snap, p.id)
    if (used.length) { store.refuse(t('gui.settings.providers.in_use', { roles: used.map(roleName).join(', ') })); return }
    void store.run(busy(p.id), () => store.source().provider('disconnect', { slug: p.id }))
  }
  const save = (): void => {
    const k = key.trim()
    const b = base.trim()
    if (needsKey(p) && !k && !p.on) { store.refuse(t('gui.settings.providers.key_first')); return }
    if (takesBase(p) && !b) { store.refuse(t('gui.settings.providers.base_first')); return }
    const params: Record<string, unknown> = { slug: p.id }
    if (k) params.api_key = k
    if (b) params.api_base = b
    void store.run(busy(p.id), () => store.source().provider('save_key', params)).then((ok) => { if (ok) setKey('') })
  }
  const setBaseField = (): void => {
    const b = base.trim()
    if (b === (p.apiBase || '')) return
    void store.run(busy(p.id), () => store.source().setFields(p.id, { api_base: b }))
  }
  const btn = p.on ? t('gui.settings.update') : t('gui.settings.providers.connect')
  const off = p.on && <button type="button" className="mini ghost" onClick={disconnect}>{t('gui.settings.providers.disconnect')}</button>
  return (
    <Card title={t('gui.settings.providers.connection')}>
      {kind === 'oauth' && (
        <>
          <Row label={t('gui.settings.providers.account')}>
            <span className="settings-taglist">
              {p.on && <Rov>{t('gui.settings.providers.authorized')}</Rov>}
              <button type="button" className={p.on ? 'mini ghost' : 'mini'} onClick={() => void store.oauthStart(p.id)}>
                {p.on ? t('gui.settings.providers.reauth') : t('gui.settings.providers.auth_browser')}
              </button>
              {off}
              <OauthNote slug={p.id} />
            </span>
          </Row>
          <Row label={t('gui.settings.providers.billing')}><Rov>{t('gui.settings.providers.subscription')}</Rov></Row>
        </>
      )}
      {kind !== 'oauth' && !needsKey(p) && takesBase(p) && (
        <Row stack label={t('gui.settings.providers.base')}>
          <span className="settings-taglist">
            <input className="settings-tbox" value={base} aria-label={t('gui.settings.providers.base')} placeholder="http://localhost:11434"
              onChange={(e) => setBase(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
            <button type="button" className="mini" disabled={store.isBusy(busy(p.id))} onClick={save}>{btn}</button>
            {off}
          </span>
        </Row>
      )}
      {kind !== 'oauth' && takesKey(p) && (
        <Row stack label={<>{t(needsKey(p) ? 'gui.settings.providers.api_key' : 'gui.settings.providers.api_key_optional')}<KeyLink url={p.keyUrl} /></>}>
          <span className="settings-taglist">
            <KeyInput className="settings-tbox" value={key} aria-label={t('gui.settings.providers.api_key')}
              placeholder={p.on ? t('gui.settings.key_set_ph') : t('gui.settings.providers.paste_key')}
              onChange={(e) => setKey(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
            {needsKey(p) && <button type="button" className="mini" disabled={store.isBusy(busy(p.id))} onClick={save}>{btn}</button>}
            {!needsKey(p) && <button type="button" className="mini ghost" disabled={store.isBusy(busy(p.id))} onClick={save}>{t('gui.settings.update')}</button>}
            {needsKey(p) && off}
          </span>
        </Row>
      )}
      {kind !== 'oauth' && !takesKey(p) && (
        <Row stack label={t('gui.settings.providers.base')}>
          <span className="settings-taglist">
            <input className="settings-tbox" value={base} aria-label={t('gui.settings.providers.base')} placeholder="http://localhost:11434"
              onChange={(e) => setBase(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
            <button type="button" className="mini" disabled={store.isBusy(busy(p.id))} onClick={save}>{btn}</button>
            {off}
          </span>
        </Row>
      )}
      {kind !== 'oauth' && needsKey(p) && (takesBase(p) || p.kind === 'endpoint') && (
        <Row label={t('gui.settings.providers.base')}>
          <input className="settings-tbox" value={base} aria-label={t('gui.settings.providers.base')}
            placeholder={p.needsBase ? 'https://' : t('gui.settings.providers.base_default')}
            onChange={(e) => setBase(e.currentTarget.value)} onBlur={setBaseField} onKeyDown={(e) => { if (e.key === 'Enter') setBaseField() }} />
        </Row>
      )}
      {p.id === AZURE && <AzureFields p={p} />}
    </Card>
  )
}

/* The provider's own config section, for the fields model.options does not
   carry (Azure's deployment and API version, an address it left out). */
function rawSection(raw: Record<string, unknown>, slug: string): Record<string, unknown> {
  const providers = raw.providers as Record<string, Record<string, unknown>> | undefined
  return (providers && providers[slug]) || {}
}
const rawStr = (raw: Record<string, unknown>, slug: string, key: string): string => {
  const v = rawSection(raw, slug)[key]
  return typeof v === 'string' ? v : ''
}

function AzureFields({ p }: { p: ProviderRow }): JSX.Element {
  const raw = store.get().snap.raw
  const [deploy, setDeploy] = useState(rawStr(raw, p.id, 'deployment'))
  const [ver, setVer] = useState(rawStr(raw, p.id, 'apiVersion'))
  const write = (fields: Record<string, string>): void => { void store.run(busy(p.id), () => store.source().setFields(p.id, fields)) }
  return (
    <>
      <Row label={t('gui.settings.providers.deployment')}>
        <input className="settings-tbox" value={deploy} aria-label={t('gui.settings.providers.deployment')} placeholder={t('gui.settings.providers.deployment_ph')}
          onChange={(e) => setDeploy(e.currentTarget.value)} onBlur={() => { if (deploy.trim() && deploy.trim() !== rawStr(store.get().snap.raw, p.id, 'deployment')) write({ deployment: deploy.trim() }) }} />
      </Row>
      <Row label={t('gui.settings.providers.api_version')}>
        <input className="settings-tbox" value={ver} aria-label={t('gui.settings.providers.api_version')} placeholder="2024-10-21"
          onChange={(e) => setVer(e.currentTarget.value)} onBlur={() => { if (ver.trim() && ver.trim() !== rawStr(store.get().snap.raw, p.id, 'apiVersion')) write({ api_version: ver.trim() }) }} />
      </Row>
    </>
  )
}

function Models({ p }: { p: ProviderRow }): JSX.Element {
  const s = store.get()
  const listed = p.configured || []
  const open = !!s.sheet && s.sheet.slug === p.id
  const remove = (m: string): void => {
    const used = rolesUsing(s.snap, p.id, m)
    if (used.length) { store.refuse(t('gui.settings.providers.model_in_use', { roles: used.map(roleName).join(', '), model: m })); return }
    void store.run(busy(p.id), () => store.source().provider('remove_model', { slug: p.id, model: m }))
  }
  return (
    <Card title={t('gui.settings.providers.models')} raw
      act={
        <button type="button" className="mini ghost" data-addmodel={p.id} aria-expanded={open}
          onClick={() => { if (open) store.set({ sheet: null }); else void store.sheetOpen(p.id) }}>
          {t('gui.settings.providers.add_model')}
        </button>
      }>
      <div className="settings-rows">
        <Row stack label={t('gui.settings.providers.models_listed')}>
          <span className="settings-taglist">
            {listed.map((m) => (
              <span key={m} className="settings-tag2">{m}<span className="settings-x" role="button" aria-label={t('gui.settings.providers.remove_model', { model: m })} onClick={() => remove(m)}>{'\u00d7'}</span></span>
            ))}
            {!listed.length && <span className="settings-rov" style={{ fontSize: 12 }}>{t('gui.settings.providers.no_models_yet')}</span>}
          </span>
        </Row>
      </div>
      {open && <AddModelPop p={p} />}
    </Card>
  )
}

function KvForm({ fields, onSave, onCancel, saveLabel }: {
  fields: Array<{ id: string; placeholder: string; secret?: boolean; options?: string[] }>
  onSave(values: Record<string, string>): void
  onCancel(): void
  saveLabel: string
}): JSX.Element {
  const [values, setValues] = useState<Record<string, string>>({})
  const at = (id: string): string => values[id] ?? ''
  const put = (id: string, v: string): void => setValues({ ...values, [id]: v })
  return (
    <div className="settings-kvform">
      {fields.map((f) => f.options ? (
        <select key={f.id} value={at(f.id) || f.options[0]} aria-label={f.placeholder} onChange={(e) => put(f.id, e.currentTarget.value)}>
          {f.options.map((o) => <option key={o}>{o}</option>)}
        </select>
      ) : (
        <input key={f.id} type={f.secret ? 'password' : 'text'} value={at(f.id)} placeholder={f.placeholder} aria-label={f.placeholder}
          autoComplete="off" onChange={(e) => put(f.id, e.currentTarget.value)} />
      ))}
      <button type="button" className="mini" onClick={() => onSave(Object.fromEntries(fields.map((f) => [f.id, (at(f.id) || (f.options ? f.options[0]! : '')).trim()])))}>{saveLabel}</button>
      <button type="button" className="mini ghost" onClick={onCancel}>{t('gui.cancel')}</button>
    </div>
  )
}

/* What the person stated about a model, from the config section itself:
   `labels` folds these into the registry's own names, so it cannot tell a
   name somebody chose from one the catalogue ships. */
function statedOverlays(raw: Record<string, unknown>, slug: string): Array<[string, { label?: string; description?: string }]> {
  const providers = raw.providers as Record<string, { modelOverlay?: Record<string, { label?: string; description?: string }> }> | undefined
  const overlay = (providers && providers[slug] && providers[slug].modelOverlay) || {}
  return Object.entries(overlay).filter(([, v]) => v && (v.label || v.description))
}

function Advanced({ p }: { p: ProviderRow }): JSX.Element {
  const s = store.get()
  const headers = Object.entries(p.headers || {})
  const overlays = statedOverlays(s.snap.raw, p.id)
  const listed = p.configured || []
  /* The address of a vendor whose connection card has no address row: an
     override of the registry's default, written on its own. */
  const [base, setBase] = useState(p.apiBase || '')
  const showBase = takesKey(p) && !takesBase(p) && p.kind !== 'endpoint'
  const setBaseField = (): void => {
    const b = base.trim()
    if (b === (p.apiBase || '')) return
    void store.run(busy(p.id), () => store.source().setFields(p.id, { api_base: b }))
  }
  const setHeader = (name: string, value: string | null): void => {
    void store.run(busy(p.id), () => store.source().setFields(p.id, { extra_headers: { [name]: value } }))
  }
  const saveHeader = (v: Record<string, string>): void => {
    if (!v.name) { store.refuse(t('gui.settings.providers.header_name_first')); return }
    if (!v.value) { store.refuse(t('gui.settings.providers.header_value_first')); return }
    if (headers.some(([n]) => n.toLowerCase() === v.name!.toLowerCase())) { store.refuse(t('gui.settings.providers.header_exists', { name: v.name })); return }
    store.set({ hdrAdd: null })
    setHeader(v.name, v.value)
  }
  const saveOverlay = (v: Record<string, string>): void => {
    if (!v.model) { store.refuse(t('gui.settings.providers.model_first')); return }
    if (!v.label) { store.refuse(t('gui.settings.providers.label_first')); return }
    store.set({ ovlAdd: null })
    void store.run(busy(p.id), () => store.source().provider('add_model', { slug: p.id, model: v.model, label: v.label, description: v.description || '' }))
  }
  const clearOverlay = (model: string): void => {
    void store.run(busy(p.id), () => store.source().provider('add_model', { slug: p.id, model, label: '', description: '' }))
  }
  return (
    <Card title={t('gui.settings.providers.advanced')}>
      {showBase && (
        <Row label={t('gui.settings.providers.base')} sub={t('gui.settings.providers.base_override')}>
          <input className="settings-tbox" value={base} aria-label={t('gui.settings.providers.base')} placeholder={p.defaultApiBase || t('gui.settings.providers.base_default')}
            onChange={(e) => setBase(e.currentTarget.value)} onBlur={setBaseField} onKeyDown={(e) => { if (e.key === 'Enter') setBaseField() }} />
        </Row>
      )}
      <Row stack label={t('gui.settings.providers.headers')}>
        <div style={{ width: '100%' }}>
          {headers.length > 0 && (
            <div className="settings-kvlist">
              {headers.map(([name, value]) => (
                <div key={name} className="settings-kvrow">
                  <span className="settings-kvn">{name}</span><span className="settings-kvv">{value}</span>
                  <IconBtn glyph="x" label={t('gui.settings.providers.remove_header', { name })} onClick={() => setHeader(name, null)} />
                </div>
              ))}
            </div>
          )}
          {s.hdrAdd === p.id ? (
            <KvForm
              fields={[{ id: 'name', placeholder: t('gui.settings.providers.header_name_ph') }, { id: 'value', placeholder: t('gui.settings.providers.header_value_ph'), secret: true }]}
              onSave={saveHeader} onCancel={() => store.set({ hdrAdd: null })} saveLabel={t('gui.add')}
            />
          ) : (
            <button type="button" className="mini ghost" onClick={() => store.set({ hdrAdd: p.id, ovlAdd: null })}>
              {headers.length ? t('gui.settings.providers.add_another') : t('gui.add')}
            </button>
          )}
        </div>
      </Row>
      <Row stack label={t('gui.settings.providers.display_names')}>
        <div style={{ width: '100%' }}>
          {overlays.length > 0 && (
            <div className="settings-kvlist">
              {overlays.map(([model, v]) => (
                <div key={model} className="settings-kvrow">
                  <span className="settings-kvn">{model}</span>
                  <span className="settings-kvv" style={{ color: 'var(--text)' }}>{v.label || model}{v.description ? ` · ${v.description}` : ''}</span>
                  <IconBtn glyph="x" label={t('gui.settings.providers.remove_label', { model })} onClick={() => clearOverlay(model)} />
                </div>
              ))}
            </div>
          )}
          {s.ovlAdd === p.id ? (
            <KvForm
              fields={[
                listed.length ? { id: 'model', placeholder: t('gui.settings.providers.model_id'), options: listed } : { id: 'model', placeholder: t('gui.settings.providers.model_id') },
                { id: 'label', placeholder: t('gui.settings.providers.label_ph') },
                { id: 'description', placeholder: t('gui.settings.providers.description_ph') },
              ]}
              onSave={saveOverlay} onCancel={() => store.set({ ovlAdd: null })} saveLabel={t('gui.add')}
            />
          ) : (
            <button type="button" className="mini ghost" onClick={() => store.set({ ovlAdd: p.id, hdrAdd: null })}>
              {overlays.length ? t('gui.settings.providers.add_another') : t('gui.add')}
            </button>
          )}
        </div>
      </Row>
    </Card>
  )
}

/* The head of the pane: the vendor, where to reach it, and one line of state.
   The breadcrumb this replaces was the way back to a list that is now beside
   it -- on a two-column page there is nothing to go back to. */
function Head({ p }: { p: ProviderRow }): JSX.Element {
  const state = p.on
    ? t('gui.settings.providers.connected')
    : t(kindOf(p) === 'oauth' ? 'gui.settings.providers.needs_auth' : kindOf(p) === 'local' ? 'gui.settings.providers.needs_base' : 'gui.settings.providers.needs_key')
  const link = p.keyUrl || p.homepage
  return (
    <div className="settings-tp-head">
      <ProviderIcon id={p.id} name={p.name} />
      <div className="settings-tp-ttl">
        <div className="settings-tp-name">
          {p.name}
          {link ? <a className="settings-exlink" href={link} target="_blank" rel="noopener" aria-label={p.name}>{'\u2197'}</a> : null}
          <Tag>{kindLabel(p)}</Tag>
        </div>
        <div className={p.on ? 'settings-tp-state settings-tp-live' : 'settings-tp-state'}>{state}</div>
      </div>
    </div>
  )
}

export function ProviderDetail({ slug }: { slug: string }): JSX.Element | null {
  const s = store.get()
  const p = s.snap.providers.find((x) => x.id === slug)
  if (!p) { store.set({ provider: null }); return null }
  /* One element, because the page around it is a two-column grid: a fragment
     put every card in it into a column of its own. */
  return (
    <div className="settings-tp-main">
      <Head p={p} />
      <Connection p={p} />
      <Models p={p} />
      {kindOf(p) !== 'oauth' && <Advanced p={p} />}
    </div>
  )
}
