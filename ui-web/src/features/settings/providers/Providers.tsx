/* The provider list and the inline "add a provider" block. Connected
   providers only; adding one picks a vendor and connects it in one card. */
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { KeyInput } from '../../../components/KeyInput'
import { ProviderIcon } from '../../../components/ProviderMark'
import { t } from '../../../i18n/t'
import { Card, Chip, KeyLink, Row, Rov, Tag } from '../Fields'
import * as store from '../store'

import type { ProvFilter } from '../store'
import type { ConnectProbe, ProviderRow } from '../types'
import type { JSX, KeyboardEvent } from 'react'

export const AZURE = 'azure_openai'

/* The registry's auth shapes, in the order the picker groups them. */
const KINDS: Array<[string, string]> = [
  ['key', 'gui.settings.providers.kind_key'],
  ['oauth', 'gui.settings.providers.kind_oauth'],
  ['local', 'gui.settings.providers.kind_local'],
]
export const kindOf = (p: ProviderRow): string => (p.kind === 'oauth' ? 'oauth' : p.kind === 'local' || p.kind === 'endpoint' ? 'local' : 'key')
export const kindLabel = (p: ProviderRow): string => t((KINDS.find(([k]) => k === kindOf(p)) || KINDS[0]!)[1])
/* The four shape buckets, defined here and read by the catalogue page's filter
   too so the two cannot drift: an aggregator first, since it resells whatever
   shape it takes, then the sign-in and self-hosted shapes, and everything else
   is a vendor held directly -- an endpoint credential too. */
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

/* The vendor dropdown. Not a native <select>: fifty-odd vendors open as a
   popup the height of the screen, and a native popup's height is the OS's to
   decide. The list is portalled to the body because the card it sits in clips
   its overflow, and flips above the field when there is no room below. */
const LIST_H = 300

function VendorPick({ rows, value, onPick }: { rows: ProviderRow[]; value: string; onPick: (id: string) => void }): JSX.Element {
  const btn = useRef<HTMLButtonElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const [at, setAt] = useState<{ left: number; width: number; top?: number; bottom?: number } | null>(null)
  const cur = rows.find((x) => x.id === value)
  const close = (): void => setAt(null)
  const open = (): void => {
    const r = btn.current!.getBoundingClientRect()
    const below = window.innerHeight - r.bottom
    const width = Math.max(r.width, 240)
    setAt(below < LIST_H + 12 && r.top > below
      ? { left: r.left, width, bottom: window.innerHeight - r.top + 4 }
      : { left: r.left, width, top: r.bottom + 4 })
  }
  const pick = (id: string): void => { close(); btn.current?.focus(); if (id !== value) onPick(id) }
  useEffect(() => {
    if (!at) return
    const box = list.current
    const sel = box?.querySelector<HTMLElement>('[aria-selected="true"]')
    if (box && sel) box.scrollTop = sel.offsetTop - (box.clientHeight - sel.offsetHeight) / 2
    sel?.focus({ preventScroll: true })
    const inside = (n: EventTarget | null): boolean => !!n && (!!list.current?.contains(n as Node) || !!btn.current?.contains(n as Node))
    const away = (e: Event): void => { if (!inside(e.target)) close() }
    const moved = (e: Event): void => { if (!list.current?.contains(e.target as Node)) close() }
    /* Captured at the document so the Escape that closes the list is not also
       the one that closes the dialog around it. */
    const esc = (e: globalThis.KeyboardEvent): void => {
      if (e.key !== 'Escape') return
      e.preventDefault(); e.stopPropagation(); close(); btn.current?.focus()
    }
    document.addEventListener('mousedown', away, true)
    document.addEventListener('keydown', esc, true)
    window.addEventListener('scroll', moved, true)
    window.addEventListener('resize', close)
    return () => {
      document.removeEventListener('mousedown', away, true)
      document.removeEventListener('keydown', esc, true)
      window.removeEventListener('scroll', moved, true)
      window.removeEventListener('resize', close)
    }
  }, [at])
  const onKey = (e: KeyboardEvent): void => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const opts = [...(list.current?.querySelectorAll<HTMLElement>('[role="option"]') || [])]
    const i = opts.indexOf(document.activeElement as HTMLElement)
    opts[Math.min(opts.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)))]?.focus()
  }
  return (
    <>
      <button ref={btn} type="button" className="sel settings-vpick" aria-haspopup="listbox" aria-expanded={!!at}
        aria-label={t('gui.settings.providers.vendor')}
        onClick={() => (at ? close() : open())}
        onKeyDown={(e) => { if ((e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') && !at) { e.preventDefault(); open() } }}>
        {cur ? <><ProviderIcon id={cur.id} name={cur.name} /><span>{cur.name}</span></> : null}
      </button>
      {at && createPortal(
        <div ref={list} className="settings-vlist" role="listbox" aria-label={t('gui.settings.providers.vendor')} onKeyDown={onKey}
          style={{ left: at.left, width: at.width, top: at.top, bottom: at.bottom, maxHeight: LIST_H }}>
          {GROUPS.map(([kind, label]) => {
            const group = rows.filter((x) => groupOf(x) === kind)
            return group.length ? (
              <div key={kind} role="group" aria-label={t(label)}>
                <div className="settings-vgrp">{t(label)}</div>
                {group.map((x) => (
                  <button key={x.id} type="button" role="option" aria-selected={x.id === value} className="settings-vopt" onClick={() => pick(x.id)}>
                    <ProviderIcon id={x.id} name={x.name} /><span>{x.name}</span>
                  </button>
                ))}
              </div>
            ) : null
          })}
        </div>,
        document.body,
      )}
    </>
  )
}

/* What a credential check means to the reader, one sentence per outcome.
   Only a rejection at connect time refuses the key; everything else was
   stored and says what is still worth knowing. */
const PROBE_TONE: Record<string, 'ok' | 'warn' | 'muted'> = {
  valid: 'ok', no_probe_endpoint: 'muted', key_unchecked: 'muted',
}
const probeTone = (p: ConnectProbe | undefined): 'ok' | 'warn' | 'muted' | null =>
  p ? PROBE_TONE[p.status] || 'warn' : null
function probeText(p: ConnectProbe): string {
  switch (p.status) {
    case 'valid':
      return p.models_count
        ? t('gui.settings.providers.probe_valid', { n: p.models_count })
        : t('gui.settings.providers.probe_valid_plain')
    case 'invalid_key': return t('gui.settings.providers.probe_invalid_saved')
    case 'no_credits': return t('gui.settings.providers.probe_no_credits')
    case 'rate_limited': return t('gui.settings.providers.probe_rate_limited')
    case 'network_error': return t('gui.settings.providers.probe_unreachable')
    case 'proxy_unreachable': return t('gui.settings.providers.probe_proxy')
    case 'no_probe_endpoint': return t('gui.settings.providers.probe_no_endpoint')
    case 'key_unchecked': return t('gui.settings.providers.probe_unchecked')
    default: return t('gui.settings.providers.probe_unknown', { status: p.status })
  }
}

/* A connected provider's test: "testing" while it runs, then its verdict,
   with a way to ask again when the answer was anything but a plain yes.
   Nothing when it was not tested this session. */
export function ProbeNote({ slug }: { slug: string }): JSX.Element | null {
  const probe = store.get().probes[slug]
  const busy = store.isBusy(`probe:${slug}`)
  if (busy) return <span className="settings-pnote" data-tone="muted">{t('gui.settings.providers.probe_checking')}</span>
  if (!probe) return null
  const again = PROBE_TONE[probe.status] === undefined
  return (
    <span className="settings-pnote" data-tone={probeTone(probe)!} title={probe.error || undefined}>
      {probeText(probe)}
      {again && (
        <button type="button" className="settings-plink" onClick={() => void store.recheck(slug)}>
          {t('gui.settings.providers.probe_recheck')}
        </button>
      )}
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
    const params: Record<string, unknown> = { slug: p.id }
    if (k) params.api_key = k
    if (b) params.api_base = b
    void (async () => {
      if (!await store.connect(`connect:${p.id}`, p.id, params)) return
      if (p.id === AZURE && (deploy.trim() || ver.trim())) {
        await store.run(`connect:${p.id}`, () => store.source().setFields(p.id, { deployment: deploy.trim(), api_version: ver.trim() }))
      }
      close()
    })()
  }
  const saving = store.isBusy(`connect:${p.id}`)
  return (
    <div className="settings-cfg settings-padd">
      <Row label={t('gui.settings.providers.vendor')}>
        <span className="selw">
          <VendorPick rows={rows} value={p.id} onPick={(id) => store.set({ provAdd: id })} />
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
                onChange={(e) => setKey(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !saving) connect() }} />
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
            <button type="button" className="mini" disabled={saving} onClick={connect}>
              {t('gui.settings.providers.connect')}
            </button>
          )}
          {s.err && <span className="settings-padderr" role="alert">{s.err}</span>}
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
      {/* Keyed by the vendor: the form seeds its address from the one it mounted
          on, and a role row can point an already open form at another vendor. */}
      {s.provAdd !== null && <AddBlock key={s.provAdd} slug={s.provAdd} hideCancel={setup && !on.length} />}
      {!on.length && s.provAdd === null && <div className="settings-rows"><Row><Rov>{t('gui.settings.providers.none')}</Rov></Row></div>}
      {on.map((p) => (
        <div key={p.id} className="settings-prow2">
          <Chip state={!store.isBusy(`probe:${p.id}`) && probeTone(s.probes[p.id]) === 'warn' ? 'warn' : 'on'}>{t('gui.settings.providers.connected')}</Chip>
          <span className="settings-pn2">{p.name}</span>
          {kindOf(p) === 'oauth' && <Tag>{t('gui.settings.providers.subscription')}</Tag>}
          {(p.configured || []).length > 0 && <span className="settings-kk">{t('gui.settings.providers.n_models', { n: (p.configured || []).length })}</span>}
          <ProbeNote slug={p.id} />
          <span style={{ flex: 1 }} />
          {setup ? (
            <button type="button" className="mini ghost" disabled={store.isBusy(`disconnect:${p.id}`)}
              onClick={() => void store.run(`disconnect:${p.id}`, () => store.source().provider('disconnect', { slug: p.id })).then((ok) => { if (ok) store.dropProbe(p.id) })}>
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
