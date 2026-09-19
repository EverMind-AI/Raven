/* Tools: the built-in tools in eight groups, one switch each over
   tools.disabledTools, a "needs setup" badge where a key or model is still
   missing, and a panel on the rows that have something to configure. */
import { useState } from 'react'

import { KeyInput } from '../../../components/KeyInput'
import { t } from '../../../i18n/t'
import { Card, Chip, KeyLink, Row, Rov, Switch, Xrow } from '../Fields'
import { ROLES, RolePill, disabledTools, roleValue } from '../providers/Roles'
import { FETCH_KEYLESS, WEB_VENDOR, WEB_VENDOR_LABEL, WEB_VENDOR_URL, keySet, legacyKey, str, vendorKey, webVendor } from '../source'
import * as store from '../store'
import GROUPS from '../toolGroups.json'

import type { Role } from '../providers/Roles'
import type { ToolRow } from '../types'
import type { JSX } from 'react'

export const TOOL_GROUPS = GROUPS as Record<string, string[]>
/* The meta tools the model uses to find the rest: built in, not switchable. */
export const META_GROUP = 'search'

const GROUP_LABEL: Record<string, string> = {
  file: 'gui.settings.tools.grp_file', run: 'gui.settings.tools.grp_run', net: 'gui.settings.tools.grp_net',
  generate: 'gui.settings.tools.grp_generate', collab: 'gui.settings.tools.grp_collab', skills: 'gui.settings.tools.grp_skills',
  memory: 'gui.settings.tools.grp_memory', search: 'gui.settings.tools.grp_search',
}

const ROLE_OF: Record<string, string> = {
  image_generate: 'image', text_to_speech: 'speech', video_generate: 'video', understand_media: 'multimodal',
}

/* Why a wanted tool would still not register, or '' when nothing stops it:
   the web tools by their vendor's key, deep research by its key, the media
   tools by their role. */
export function blocker(id: string, raw: Record<string, unknown>): string {
  const snap = store.get().snap
  if (id === 'web_search') return keySet(id, webVendor(id, raw), raw) ? '' : 'key'
  if (id === 'web_fetch') {
    const vendor = webVendor(id, raw)
    return !FETCH_KEYLESS.has(vendor) && !keySet(id, vendor, raw) ? 'key' : ''
  }
  if (id === 'deep_research') return str(raw, 'tools.deepResearch.apiKey') ? '' : 'key'
  const roleId = ROLE_OF[id]
  if (roleId) {
    const role = ROLES.find((r) => r.id === roleId) as Role
    return roleValue(role, snap) ? '' : 'model'
  }
  return ''
}

export function KeyRow({ label, keyName, legacy, url, raw }: {
  label: string
  keyName: string
  /* The pre-vendor leaf the same key may still sit in; a clear empties it too. */
  legacy?: string | null
  url?: string
  raw: Record<string, unknown>
}): JSX.Element {
  const [value, setValue] = useState('')
  const isSet = !!str(raw, keyName) || (!!legacy && !!str(raw, legacy))
  const save = (): void => {
    const v = value.trim()
    if (!v) { store.refuse(t('gui.settings.tools.key_first')); return }
    void store.write(keyName, v)
  }
  const clear = async (): Promise<void> => {
    if (str(raw, keyName)) await store.write(keyName, '')
    if (legacy && str(raw, legacy)) await store.write(legacy, '')
  }
  return (
    <Row stack label={<>{label}<KeyLink url={url} /></>}>
      <span className="settings-taglist">
        <KeyInput className="settings-tbox" value={value} aria-label={label}
          placeholder={isSet ? t('gui.settings.key_set_ph') : t('gui.settings.key_ph')}
          onChange={(e) => setValue(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
        <button type="button" className="mini" onClick={save}>{isSet ? t('gui.settings.update') : t('gui.save')}</button>
        {isSet && <button type="button" className="mini ghost" onClick={() => void clear()}>{t('gui.settings.clear')}</button>}
      </span>
    </Row>
  )
}

export function VendorSelect({ keyName, value, opts }: { keyName: string; value: string; opts: Array<[string, string]> }): JSX.Element {
  return (
    <span className="settings-selw">
      <select className="settings-sel" value={value} aria-label={t('gui.settings.tools.vendor')} onChange={(e) => void store.write(keyName, e.currentTarget.value)}>
        {opts.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
      </select>
    </span>
  )
}

function Panel({ id, raw }: { id: string; raw: Record<string, unknown> }): JSX.Element | null {
  const roleId = ROLE_OF[id]
  if (roleId) {
    const role = ROLES.find((r) => r.id === roleId) as Role
    return <Row label={t('gui.settings.tools.model')}><RolePill role={role} /></Row>
  }
  if (id === 'web_search' || id === 'web_fetch') {
    const pick = WEB_VENDOR[id]!
    const vendor = webVendor(id, raw)
    const keyed = id === 'web_search' || !FETCH_KEYLESS.has(vendor)
    return (
      <>
        <Row label={t(id === 'web_search' ? 'gui.settings.tools.search_vendor' : 'gui.settings.tools.fetch_vendor')}>
          <VendorSelect keyName={pick.path} value={vendor} opts={pick.vendors.map((v) => [v, WEB_VENDOR_LABEL[v] || v])} />
        </Row>
        {(keyed || vendor === 'jina') && (
          <KeyRow label={t('gui.settings.tools.vendor_key', { name: WEB_VENDOR_LABEL[vendor] || vendor })}
            keyName={vendorKey(vendor)} legacy={legacyKey(id, vendor)} url={WEB_VENDOR_URL[vendor]} raw={raw} />
        )}
      </>
    )
  }
  if (id === 'deep_research') {
    return <KeyRow label={t('gui.settings.tools.vendor_key', { name: 'MiroThinker' })} keyName="tools.deepResearch.apiKey" raw={raw} />
  }
  return null
}

const hasPanel = (id: string): boolean => !!ROLE_OF[id] || ['web_search', 'web_fetch', 'deep_research'].includes(id)

export function Tools(): JSX.Element {
  const s = store.get()
  const raw = s.snap.raw
  const known = new Map(s.snap.tools.map((row) => [row.id, row]))
  const disabled = disabledTools(raw)
  const wanted = (id: string): boolean => !disabled.includes(id)
  const flip = (id: string, on: boolean): void => {
    const next = on ? disabled.filter((x) => x !== id) : [...disabled, id]
    /* Turning on a tool that still lacks its key or model opens its panel, so
       the switch lands the reader where the missing piece goes. */
    if (on && blocker(id, raw)) store.set({ toolOpen: id })
    void store.write('tools.disabledTools', next)
  }
  let on = 0
  let total = 0
  for (const [group, ids] of Object.entries(TOOL_GROUPS)) {
    if (group === META_GROUP) continue
    for (const id of ids) {
      if (!known.has(id)) continue
      total += 1
      if (wanted(id) && !blocker(id, raw)) on += 1
    }
  }
  const row = (id: string, meta: boolean): JSX.Element | null => {
    const known_ = known.get(id) as ToolRow | undefined
    if (!known_ && !meta) return null
    const isOn = !meta && wanted(id)
    const blk = isOn ? blocker(id, raw) : ''
    return (
      <Xrow
        key={id}
        name={<span className="mono">{id}</span>}
        status={meta
          ? <Rov>{t('gui.settings.tools.builtin')}</Rov>
          : blk ? <Chip state="warn">{t('gui.settings.tools.setup')}</Chip> : null}
        ctl={meta ? <span className="settings-swi" role="switch" aria-checked aria-disabled="true" aria-label={id} /> : <Switch on={isOn} label={id} onChange={(v) => flip(id, v)} />}
        panel={!meta && hasPanel(id) ? <Panel id={id} raw={raw} /> : undefined}
        open={s.toolOpen === id}
        dim={meta || !isOn}
        onToggle={() => store.set({ toolOpen: s.toolOpen === id ? null : id })}
      />
    )
  }
  return (
    <>
      <div className="settings-crumb"><Rov>{t('gui.settings.tools.counter', { on, total })}</Rov></div>
      {Object.entries(TOOL_GROUPS).map(([group, ids]) => (
        <Card key={group} title={t(GROUP_LABEL[group] || 'gui.settings.tools.grp_file')}>
          {ids.map((id) => row(id, group === META_GROUP))}
        </Card>
      ))}
    </>
  )
}
