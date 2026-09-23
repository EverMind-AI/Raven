/* One provider: its connection, the models it lists (with the vendor's own
   list to add from), and the advanced card -- address, headers, display
   names. The refusals are the page's: a provider or model a role uses stays. */
import { useState } from 'react'

import { KeyInput } from '../../../components/KeyInput'
import { ProviderIcon } from '../../../components/ProviderMark'
import { t } from '../../../i18n/t'
import { Fold, IconBtn, Rov, Sec } from '../Fields'
import * as store from '../store'
import { ownId } from './AddModelPop'
import { AZURE, OauthNote, kindOf, needsKey, takesBase, takesKey } from './Providers'
import { roleName, rolesUsing } from './Roles'

import type { ProviderRow } from '../types'
import type { JSX } from 'react'

const busy = (slug: string): string => `prov:${slug}`

/* Where the key comes from, named. A bare arrow beside "API key" does not say
   that what it opens is the page that issues one. */
function KeyGet({ url }: { url?: string | null }): JSX.Element | null {
  if (!url) return null
  return (
    <a className="exlink" href={url} target="_blank" rel="noopener">
      {t('gui.settings.get_key')}
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17 17 7M9 7h8v8" /></svg>
    </a>
  )
}

/* The address a provider is reached at, wherever it is drawn.
 *
 * An aggregator's address is a first-class field -- you point one at a proxy or
 * a regional host the way you would type a key -- so it sits in the body beside
 * the key. Every other vendor has one address that works, and a box for
 * overriding it belongs under Advanced with the other things few people touch.
 * Both need the same control, and the same way back: a reset appears once the
 * value differs from what the registry ships, because an address typed by hand
 * is not one you can retype from memory. */
function AddressRow({ p, label, sub, placeholder }: {
  p: ProviderRow
  /* An aggregator's is the API address; an override of a direct vendor's own is
     its service address. Same control, two things to call it. */
  label?: string
  sub?: string
  placeholder: string
}): JSX.Element {
  const [base, setBase] = useState(p.apiBase || '')
  const write = (value: string): void => {
    void store.run(busy(p.id), () => store.source().setFields(p.id, { api_base: value }))
  }
  const commit = (): void => {
    const b = base.trim()
    if (b === (p.apiBase || '')) return
    write(b)
  }
  const shipped = p.defaultApiBase || ''
  const resettable = !!shipped && (p.apiBase || '') !== '' && (p.apiBase || '') !== shipped
  return (
    <Sec label={label ?? t('gui.settings.providers.base')} sub={sub} tight>
      <span className="settings-taglist">
        <input className="settings-tbox" value={base} aria-label={t('gui.settings.providers.base')} placeholder={placeholder}
          onChange={(e) => setBase(e.currentTarget.value)} onBlur={commit} onKeyDown={(e) => { if (e.key === 'Enter') commit() }} />
        {resettable && (
          <button type="button" className="mini ghost" aria-label={t('gui.settings.providers.base_reset')}
            onClick={() => { setBase(''); write('') }}>
            {t('gui.settings.providers.base_reset')}
          </button>
        )}
      </span>
    </Sec>
  )
}

function Connection({ p }: { p: ProviderRow }): JSX.Element {
  const [key, setKey] = useState('')
  const [base, setBase] = useState(p.apiBase || rawStr(store.get().snap.raw, p.id, 'apiBase') || p.defaultApiBase || '')
  const kind = kindOf(p)
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
  const btn = p.on ? t('gui.settings.update') : t('gui.settings.providers.connect')
  return (
    <>
      {kind === 'oauth' && (
        <>
          <Sec label={t('gui.settings.providers.account')}>
            <span className="settings-taglist">
              {p.on && <Rov>{t('gui.settings.providers.authorized')}</Rov>}
              <button type="button" className={p.on ? 'mini ghost' : 'mini'} onClick={() => void store.oauthStart(p.id)}>
                {p.on ? t('gui.settings.providers.reauth') : t('gui.settings.providers.auth_browser')}
              </button>
                <OauthNote slug={p.id} />
            </span>
          </Sec>
          <Sec label={t('gui.settings.providers.billing')}><Rov>{t('gui.settings.providers.subscription')}</Rov></Sec>
        </>
      )}
      {kind !== 'oauth' && !needsKey(p) && takesBase(p) && (
        <Sec label={t('gui.settings.providers.base')}>
          <span className="settings-taglist">
            <input className="settings-tbox" value={base} aria-label={t('gui.settings.providers.base')} placeholder="http://localhost:11434"
              onChange={(e) => setBase(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
            <button type="button" className="mini" disabled={store.isBusy(busy(p.id))} onClick={save}>{btn}</button>
          </span>
        </Sec>
      )}
      {kind !== 'oauth' && takesKey(p) && (
        <Sec label={<>{t(needsKey(p) ? 'gui.settings.providers.api_key' : 'gui.settings.providers.api_key_optional')}<KeyGet url={p.keyUrl} /></>}>
          <span className="settings-taglist">
            <KeyInput className="settings-tbox" value={key} aria-label={t('gui.settings.providers.api_key')}
              placeholder={p.on ? t('gui.settings.key_set_ph') : t('gui.settings.providers.paste_key')}
              onChange={(e) => setKey(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
            {needsKey(p) && <button type="button" className="mini" disabled={store.isBusy(busy(p.id))} onClick={save}>{btn}</button>}
            {!needsKey(p) && <button type="button" className="mini ghost" disabled={store.isBusy(busy(p.id))} onClick={save}>{t('gui.settings.update')}</button>}
          </span>
        </Sec>
      )}
      {/* The address for a provider that takes no key -- unless the block above
          has already drawn it: a local server that takes an address and no
          key met both conditions and showed "Server address" twice, each with
          its own Connect. */}
      {kind !== 'oauth' && !takesKey(p) && !(!needsKey(p) && takesBase(p)) && (
        <Sec label={t('gui.settings.providers.base')}>
          <span className="settings-taglist">
            <input className="settings-tbox" value={base} aria-label={t('gui.settings.providers.base')} placeholder="http://localhost:11434"
              onChange={(e) => setBase(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
            <button type="button" className="mini" disabled={store.isBusy(busy(p.id))} onClick={save}>{btn}</button>
          </span>
        </Sec>
      )}
      {kind !== 'oauth' && needsKey(p) && (takesBase(p) || p.gateway || p.kind === 'endpoint') && (
        <AddressRow p={p} label={t('gui.settings.providers.api_base')}
          placeholder={p.needsBase ? 'https://' : (p.defaultApiBase || t('gui.settings.providers.base_default'))} />
      )}
      {p.id === AZURE && <AzureFields p={p} />}
    </>
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
      <Sec label={t('gui.settings.providers.deployment')} tight>
        <input className="settings-tbox" value={deploy} aria-label={t('gui.settings.providers.deployment')} placeholder={t('gui.settings.providers.deployment_ph')}
          onChange={(e) => setDeploy(e.currentTarget.value)} onBlur={() => { if (deploy.trim() && deploy.trim() !== rawStr(store.get().snap.raw, p.id, 'deployment')) write({ deployment: deploy.trim() }) }} />
      </Sec>
      <Sec label={t('gui.settings.providers.api_version')} tight>
        <input className="settings-tbox" value={ver} aria-label={t('gui.settings.providers.api_version')} placeholder="2024-10-21"
          onChange={(e) => setVer(e.currentTarget.value)} onBlur={() => { if (ver.trim() && ver.trim() !== rawStr(store.get().snap.raw, p.id, 'apiVersion')) write({ api_version: ver.trim() }) }} />
      </Sec>
    </>
  )
}

const MODELS_FOLDED = 8

function Models({ p }: { p: ProviderRow }): JSX.Element {
  const s = store.get()
  const listed = p.configured || []
  const open = !!s.sheet && s.sheet.slug === p.id
  const all = s.modelsAll === p.id
  const remove = (m: string): void => {
    const used = rolesUsing(s.snap, p.id, m)
    if (used.length) { store.refuse(t('gui.settings.providers.model_in_use', { roles: used.map(roleName).join(', '), model: m })); return }
    void store.run(busy(p.id), () => store.source().provider('remove_model', { slug: p.id, model: m }))
  }
  return (
    <Sec
      label={t('gui.settings.providers.models')}
      act={
        <button type="button" className="mini ghost" data-addmodel={p.id} aria-expanded={open}
          onClick={() => { if (open) store.set({ sheet: null }); else void store.sheetOpen(p.id) }}>
          {t('gui.settings.providers.add_model')}
        </button>
      }>
      {/* The chips are the list -- a second heading over them ("models
          available") said what the section's own label already says.
          Folded past a few: a gateway takes models by the dozen, and a list
          that only ever grew pushed everything under it off the pane. A fold,
          not a scrolling box -- the pane is itself the scroller, and a second
          one inside it is a place the wheel gets stuck. Each chip names the
          model as the provider's own list does, without the provider's name in
          front of every one; the full id is its title. */}
      <span className="settings-taglist">
        {(all ? listed : listed.slice(0, MODELS_FOLDED)).map((m) => (
          <span key={m} className="settings-tag2" title={m}>{ownId(p, m)}<span className="settings-x" role="button" aria-label={t('gui.settings.providers.remove_model', { model: m })} onClick={() => remove(m)}>{'\u00d7'}</span></span>
        ))}
        {listed.length > MODELS_FOLDED && (
          <button type="button" className="settings-tagmore" aria-expanded={all}
            onClick={() => store.set({ modelsAll: all ? null : p.id })}>
            {all ? t('gui.settings.providers.models_less') : t('gui.settings.providers.models_more', { n: String(listed.length - MODELS_FOLDED) })}
          </button>
        )}
        {!listed.length && <span className="settings-tp-empty">{t('gui.settings.providers.no_models_yet')}</span>}
      </span>
    </Sec>
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
  const showBase = takesKey(p) && !takesBase(p) && !p.gateway && p.kind !== 'endpoint'
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
    <Fold open={s.adv === p.id} label={t('gui.settings.providers.advanced')}
      onToggle={() => store.set({ adv: s.adv === p.id ? null : p.id })}>
      {showBase && (
        <AddressRow p={p} sub={t('gui.settings.providers.base_override')}
          placeholder={p.defaultApiBase || t('gui.settings.providers.base_default')} />
      )}
      <Sec label={t('gui.settings.providers.headers')} tight>
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
      </Sec>
      <Sec label={t('gui.settings.providers.display_names')} tight>
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
      </Sec>
    </Fold>
  )
}

/* The head of the pane: the vendor, where to reach it, and one line of state.
   The breadcrumb this replaces was the way back to a list that is now beside
   it -- on a two-column page there is nothing to go back to. */
function Head({ p }: { p: ProviderRow }): JSX.Element {
  const state = p.on
    ? t('gui.settings.providers.connected')
    : t(kindOf(p) === 'oauth' ? 'gui.settings.providers.needs_auth' : kindOf(p) === 'local' ? 'gui.settings.providers.needs_base' : 'gui.settings.providers.needs_key')
  /* One way out of the page, not two. The name's arrow was `keyUrl ||
     homepage`, so on any provider with a key page it opened the same page as
     "Get a key" two lines below it. It stays only where the key section has no
     link of its own to offer -- a browser-authorized or local provider, or one
     the registry knows no key page for -- and then it is the vendor's site. */
  const keyLinked = kindOf(p) !== 'oauth' && takesKey(p) && !!p.keyUrl
  const link = keyLinked ? null : (p.homepage || p.keyUrl)
  return (
    <div className="settings-tp-head">
      <ProviderIcon id={p.id} name={p.name} />
      <div className="settings-tp-ttl">
        <div className="settings-tp-name">
          {p.name}
          {link ? <a className="exlink" href={link} target="_blank" rel="noopener" aria-label={p.name}>{'\u2197'}</a> : null}
        </div>
        <div className={p.on ? 'settings-tp-state settings-tp-live' : 'settings-tp-state'}>{state}</div>
      </div>
    </div>
  )
}

function Foot({ p }: { p: ProviderRow }): JSX.Element {
  const kind = kindOf(p)
  const disconnect = (): void => {
    const used = rolesUsing(store.get().snap, p.id)
    if (used.length) { store.refuse(t('gui.settings.providers.in_use', { roles: used.map(roleName).join(', ') })); return }
    void store.run(busy(p.id), () => store.source().provider('disconnect', { slug: p.id }))
  }
  return (
    <div className="settings-tp-foot">
      <button type="button" className="mini ghost danger" onClick={disconnect}>
        {t(kind === 'oauth' ? 'gui.settings.providers.disconnect_auth'
          : kind === 'local' ? 'gui.settings.providers.disconnect_local'
          : 'gui.settings.providers.disconnect_key')}
      </button>
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
      {/* Taking an account back out is not one of the fields: it stands at the
          foot under a hairline, the way every other pane in this dialog puts
          its one destructive verb. */}
      {p.on && <Foot p={p} />}
    </div>
  )
}
