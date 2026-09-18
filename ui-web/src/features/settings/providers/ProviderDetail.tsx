/* One provider: its connection, the models it lists (with the vendor's own
   list to add from), and the advanced card -- address, headers, display
   names. The refusals are the page's: a provider or model a role uses stays. */
import { useState } from 'react'

import { KeyInput } from '../../../components/KeyInput'
import { t } from '../../../i18n/t'
import { Card, Chip, Crumb, Grow, IconBtn, KeyLink, Row, Rov, Spin, Tag } from '../Fields'
import * as store from '../store'
import { AZURE, OauthNote, kindLabel, kindOf, takesBase, takesKey } from './Providers'
import { roleName, rolesUsing } from './Roles'

import type { ProviderRow } from '../types'
import type { JSX } from 'react'

const busy = (slug: string): string => `prov:${slug}`

function Connection({ p }: { p: ProviderRow }): JSX.Element {
  const [key, setKey] = useState('')
  const [base, setBase] = useState(p.apiBase || p.defaultApiBase || '')
  const kind = kindOf(p)
  const disconnect = (): void => {
    const used = rolesUsing(store.get().snap, p.id)
    if (used.length) { store.refuse(t('gui.settings.providers.in_use', { roles: used.map(roleName).join(', ') })); return }
    void store.run(busy(p.id), () => store.source().provider('disconnect', { slug: p.id }))
  }
  const save = (): void => {
    const k = key.trim()
    const b = base.trim()
    if (takesKey(p) && !k && !p.on) { store.refuse(t('gui.settings.providers.key_first')); return }
    if (!takesKey(p) && !b) { store.refuse(t('gui.settings.providers.base_first')); return }
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
      {kind !== 'oauth' && takesKey(p) && (
        <Row stack label={<>{t('gui.settings.providers.api_key')}<KeyLink url={p.keyUrl} /></>}>
          <span className="settings-taglist">
            <KeyInput className="settings-tbox" value={key} aria-label={t('gui.settings.providers.api_key')}
              placeholder={p.on ? t('gui.settings.key_set_ph') : t('gui.settings.providers.paste_key')}
              onChange={(e) => setKey(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
            <button type="button" className="mini" disabled={store.isBusy(busy(p.id))} onClick={save}>{btn}</button>
            {off}
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
      {kind !== 'oauth' && takesKey(p) && (takesBase(p) || p.kind === 'endpoint') && (
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

function AzureFields({ p }: { p: ProviderRow }): JSX.Element {
  const [deploy, setDeploy] = useState('')
  const [ver, setVer] = useState('')
  const write = (fields: Record<string, string>): void => { void store.run(busy(p.id), () => store.source().setFields(p.id, fields)) }
  return (
    <>
      <Row label={t('gui.settings.providers.deployment')}>
        <input className="settings-tbox" value={deploy} aria-label={t('gui.settings.providers.deployment')} placeholder={t('gui.settings.providers.deployment_ph')}
          onChange={(e) => setDeploy(e.currentTarget.value)} onBlur={() => { if (deploy.trim()) write({ deployment: deploy.trim() }) }} />
      </Row>
      <Row label={t('gui.settings.providers.api_version')}>
        <input className="settings-tbox" value={ver} aria-label={t('gui.settings.providers.api_version')} placeholder="2024-10-21"
          onChange={(e) => setVer(e.currentTarget.value)} onBlur={() => { if (ver.trim()) write({ api_version: ver.trim() }) }} />
      </Row>
    </>
  )
}

/* The vendor's list, filtered, plus a typed id; one click adds them all. */
function Sheet({ p }: { p: ProviderRow }): JSX.Element {
  const sheet = store.get().sheet!
  const listed = p.configured || []
  const q = sheet.q.trim()
  const ql = q.toLowerCase()
  const items = sheet.items.filter((m) => !ql || m.id.toLowerCase().includes(ql))
  const exact = sheet.items.some((m) => m.id.toLowerCase() === ql) || listed.some((m) => m.toLowerCase() === ql)
  const picks = sheet.sel.filter((m) => !listed.includes(m))
  const manual = q && !exact ? (
    <label className="settings-mli settings-add">
      <input type="checkbox" checked={sheet.sel.includes(q)} onChange={() => store.sheetToggle(q)} />
      <span className="settings-id">{q}</span><span className="settings-sd">{t('gui.settings.providers.typed')}</span>
    </label>
  ) : null
  const add = (): void => {
    void store.run(busy(p.id), () => store.source().addModels(p.id, picks)).then((ok) => { if (ok) store.set({ sheet: null }) })
  }
  return (
    <div className="settings-mlist">
      <div className="settings-mls">
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></svg>
        <input id="mlq" value={sheet.q} placeholder={t('gui.model.pick_search')} autoComplete="off" spellCheck={false} autoFocus
          onChange={(e) => store.sheetPatch({ q: e.currentTarget.value })} />
      </div>
      <div className="settings-mlb">
        {sheet.state === 'loading' && <div className="settings-mlempty"><Spin>{t('gui.settings.providers.fetching', { name: p.name })}</Spin></div>}
        {sheet.state !== 'loading' && (
          <>
            {!items.length && manual}
            {items.map((m) => {
              const has = listed.includes(m.id)
              return (
                <label key={m.id} className={has ? 'settings-mli settings-has' : 'settings-mli'}>
                  <input type="checkbox" checked={has || sheet.sel.includes(m.id)} disabled={has} onChange={() => store.sheetToggle(m.id)} />
                  <span className="settings-id">{m.id}</span>
                  {has && <span className="settings-sd">{t('gui.settings.providers.added')}</span>}
                </label>
              )
            })}
            {items.length > 0 && manual}
            {!items.length && !manual && (
              <div className="settings-mlempty">
                {sheet.state === 'failed' ? t('gui.settings.providers.no_list') : t('gui.settings.providers.no_match')}
              </div>
            )}
          </>
        )}
      </div>
      <div className="settings-mlf">
        <Rov>
          {sheet.state === 'ready' ? t('gui.settings.providers.n_available', { n: sheet.items.length }) : ''}
          {picks.length ? `${sheet.state === 'ready' ? ' · ' : ''}${t('gui.settings.providers.n_picked', { n: picks.length })}` : ''}
        </Rov>
        <Grow />
        <button type="button" className="mini ghost" onClick={() => store.set({ sheet: null })}>{t('gui.cancel')}</button>
        <button type="button" className="mini" disabled={!picks.length || store.isBusy(busy(p.id))} onClick={add}>
          {picks.length ? t('gui.settings.providers.add_n', { n: picks.length }) : t('gui.add')}
        </button>
      </div>
    </div>
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
    <Card title={t('gui.settings.providers.models')} raw>
      <div className="settings-rows">
        <Row stack label={t('gui.settings.providers.models_listed')}>
          <span className="settings-taglist">
            {listed.map((m) => (
              <span key={m} className="settings-tag2">{m}<span className="settings-x" role="button" aria-label={t('gui.settings.providers.remove_model', { model: m })} onClick={() => remove(m)}>{'×'}</span></span>
            ))}
            {!listed.length && <span className="settings-rov" style={{ fontSize: 12 }}>{t('gui.settings.providers.no_models_yet')}</span>}
            {!open && <button type="button" className="mini ghost" onClick={() => void store.sheetOpen(p.id)}>{t('gui.add')}</button>}
          </span>
        </Row>
      </div>
      {open && <Sheet p={p} />}
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

function Advanced({ p }: { p: ProviderRow }): JSX.Element {
  const s = store.get()
  const headers = Object.entries(p.headers || {})
  const overlays = Object.entries(p.labels || {}).filter(([, v]) => v && v.label)
  const listed = p.configured || []
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
                  <span className="settings-kvv" style={{ color: 'var(--text)' }}>{v.label}{v.description ? ` · ${v.description}` : ''}</span>
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

export function ProviderDetail({ slug }: { slug: string }): JSX.Element | null {
  const s = store.get()
  const p = s.snap.providers.find((x) => x.id === slug)
  if (!p) { store.set({ provider: null }); return null }
  const state: [ 'on' | 'off', string ] = p.on
    ? ['on', t('gui.settings.providers.connected')]
    : ['off', t(kindOf(p) === 'oauth' ? 'gui.settings.providers.needs_auth' : kindOf(p) === 'local' ? 'gui.settings.providers.needs_base' : 'gui.settings.providers.needs_key')]
  return (
    <>
      <Crumb back={t('gui.settings.nav.model')} onBack={() => store.set({ provider: null, sheet: null, err: '' })} name={p.name}>
        <Tag>{kindLabel(p)}</Tag>
        <Grow />
        <Chip state={state[0]}>{state[1]}</Chip>
      </Crumb>
      <Connection p={p} />
      <Models p={p} />
      {kindOf(p) !== 'oauth' && <Advanced p={p} />}
    </>
  )
}
