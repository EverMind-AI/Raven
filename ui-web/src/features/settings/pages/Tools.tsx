/* Tools: the built-in tools in eight groups, one switch each over
   tools.disabledTools, a "needs setup" badge where a key or model is still
   missing, and a panel on the rows that have something to configure. */
import { useState } from 'react'

import { KeyInput } from '../../../components/KeyInput'
import { t } from '../../../i18n/t'
import { Card, Chip, KeyLink, Row, Rov, Switch, Xrow } from '../Fields'
import { ROLES, RolePill, disabledTools, roleValue } from '../providers/Roles'
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

/* The vendors the two web tools can run on, with where each hands out keys.
   The fetch vendors other than Jina share the search vendor's key. */
export const WEB_SEARCH: Array<[string, string, string]> = [
  ['serper', 'Serper', 'https://serper.dev'], ['anysearch', 'AnySearch', 'https://anysearch.com'],
  ['serpapi', 'SerpApi', 'https://serpapi.com'], ['tavily', 'Tavily', 'https://tavily.com'],
  ['exa', 'Exa', 'https://exa.ai'], ['brave', 'Brave Search', 'https://brave.com/search/api'],
  ['firecrawl', 'Firecrawl', 'https://firecrawl.dev'],
]
export const WEB_FETCH: Array<[string, string, string, boolean]> = [
  ['jina', 'Jina Reader', 'https://jina.ai/reader', false], ['anysearch', 'AnySearch', 'https://anysearch.com', true],
  ['tavily', 'Tavily', 'https://tavily.com', true], ['exa', 'Exa', 'https://exa.ai', true],
  ['firecrawl', 'Firecrawl', 'https://firecrawl.dev', true],
]

const dig = (raw: Record<string, unknown>, path: string): unknown =>
  path.split('.').reduce<unknown>((o, k) => (o && typeof o === 'object' ? (o as Record<string, unknown>)[k] : undefined), raw)
const str = (raw: Record<string, unknown>, path: string): string => {
  const v = dig(raw, path)
  return typeof v === 'string' ? v : ''
}

const ROLE_OF: Record<string, string> = {
  image_generate: 'image', text_to_speech: 'speech', video_generate: 'video', understand_media: 'multimodal',
}

/* Why a wanted tool would still not register, or '' when nothing stops it:
   the web tools by their vendor's key, deep research by its key, the media
   tools by their role. */
export function blocker(id: string, raw: Record<string, unknown>): string {
  const snap = store.get().snap
  if (id === 'web_search') return str(raw, 'tools.web.search.apiKey') ? '' : 'key'
  if (id === 'web_fetch') {
    const vendor = str(raw, 'tools.web.fetch.provider') || 'jina'
    const row = WEB_FETCH.find((v) => v[0] === vendor)
    if (vendor === 'jina') return str(raw, 'tools.web.jinaApiKey') ? '' : 'key'
    return row && row[3] && !str(raw, 'tools.web.search.apiKey') ? 'key' : ''
  }
  if (id === 'deep_research') return str(raw, 'tools.deepResearch.apiKey') ? '' : 'key'
  const roleId = ROLE_OF[id]
  if (roleId) {
    const role = ROLES.find((r) => r.id === roleId) as Role
    return roleValue(role, snap) ? '' : 'model'
  }
  return ''
}

function KeyRow({ label, keyName, url, raw }: { label: string; keyName: string; url?: string; raw: Record<string, unknown> }): JSX.Element {
  const [value, setValue] = useState('')
  const isSet = !!str(raw, keyName)
  const save = (): void => {
    const v = value.trim()
    if (!v) { store.refuse(t('gui.settings.tools.key_first')); return }
    void store.write(keyName, v)
  }
  return (
    <Row stack label={<>{label}<KeyLink url={url} /></>}>
      <span className="settings-taglist">
        <KeyInput className="settings-tbox" value={value} aria-label={label}
          placeholder={isSet ? t('gui.settings.key_set_ph') : t('gui.settings.key_ph')}
          onChange={(e) => setValue(e.currentTarget.value)} onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
        <button type="button" className="mini" onClick={save}>{isSet ? t('gui.settings.update') : t('gui.save')}</button>
        {isSet && <button type="button" className="mini ghost" onClick={() => void store.write(keyName, '')}>{t('gui.settings.clear')}</button>}
      </span>
    </Row>
  )
}

function VendorSelect({ keyName, value, opts }: { keyName: string; value: string; opts: Array<[string, string]> }): JSX.Element {
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
  if (id === 'web_search') {
    const vendor = str(raw, 'tools.web.search.provider') || WEB_SEARCH[0]![0]
    const row = WEB_SEARCH.find((v) => v[0] === vendor) || WEB_SEARCH[0]!
    return (
      <>
        <Row label={t('gui.settings.tools.search_vendor')}>
          <VendorSelect keyName="tools.web.search.provider" value={vendor} opts={WEB_SEARCH.map((v) => [v[0], v[1]])} />
        </Row>
        <KeyRow label={t('gui.settings.tools.vendor_key', { name: row[1] })} keyName="tools.web.search.apiKey" url={row[2]} raw={raw} />
      </>
    )
  }
  if (id === 'web_fetch') {
    const vendor = str(raw, 'tools.web.fetch.provider') || 'jina'
    const row = WEB_FETCH.find((v) => v[0] === vendor) || WEB_FETCH[0]!
    return (
      <>
        <Row label={t('gui.settings.tools.fetch_vendor')}>
          <VendorSelect keyName="tools.web.fetch.provider" value={vendor} opts={WEB_FETCH.map((v) => [v[0], v[1]])} />
        </Row>
        {vendor === 'jina'
          ? <KeyRow label={t('gui.settings.tools.vendor_key', { name: row[1] })} keyName="tools.web.jinaApiKey" url={row[2]} raw={raw} />
          : row[3] && <KeyRow label={t('gui.settings.tools.vendor_key', { name: row[1] })} keyName="tools.web.search.apiKey" url={row[2]} raw={raw} />}
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
