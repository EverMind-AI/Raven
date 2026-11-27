/* Centered `#detail` modal cards for Skills, Tools, MCP servers, and Agent
 * Plugin packages. Rendered via portal into `store.detailHost()`, matching
 * Raven's centered `.detail` / `.dpanel` specification.
 */
import {
  AiContentGenerator01Icon,
  Alert02Icon,
  BrainIcon,
  Delete02Icon,
  Globe02Icon,
  Image02Icon,
  LinkSquare02Icon,
  Plug02Icon,
  PuzzleIcon,
  Video01Icon,
  VolumeHighIcon,
  Wrench01Icon,
} from '@hugeicons/core-free-icons'
import { useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { Icon } from '../../components/Icon'
import { KeyInput } from '../../components/KeyInput'
import { t } from '../../i18n/t'
import {
  MEDIA_PROVIDER,
  ROLE_KIND,
  ROLES,
  TOOL_ROLE_OF,
  disabledTools,
  everosLocked,
  followsChat,
  mediaSelection,
  providerName,
  roleProviders,
  roleValue,
  withRole,
} from '../../lib/modelRoles'
import { md } from '../../lib/prose'
import * as confirm from '../../state/confirm'
import { openPicker } from '../model/source'
import { statedTags } from '../model/types'
import {
  blocker,
  importCommand,
  mcpChip,
  mcpTone,
  pkgChip,
  pluginTone,
  skillTone,
  stripFrontmatter,
  toolTone,
} from './helpers'
import { CopyCmd, IconTile, InlineErr, KeyLink, Pill, StatusDot, Switch } from './Parts'
import {
  FETCH_KEYLESS,
  PPT_IMAGE_VENDORS,
  WEB_VENDOR,
  WEB_VENDOR_LABEL,
  WEB_VENDOR_URL,
  borrowedKey,
  legacyKey,
  str,
  vendorKey,
  webVendor,
} from './source'
import * as store from './store'

import type { Role, RoleId } from '../../lib/modelRoles'
import type { Kind, Offer } from '../model/types'
import type { AgentPluginRow, AuthField, McpSnapshot, SettingsSnapshot, SkillDetail } from './types'
import type { JSX } from 'react'

const MAIN_FILE = 'SKILL.md'

const skillDir = (path: unknown): string => String(path || '').replace(/[\\/]SKILL\.md$/, '')

const TRIGGER: Record<string, string> = {
  use_skill: 'gui.skillsTools.skills.trig_use',
  auto_inject: 'gui.skillsTools.skills.trig_auto',
  rpc: 'gui.skillsTools.skills.trig_rpc',
}

const ROLE_NAME: Partial<Record<RoleId, string>> = {
  image: 'gui.skillsTools.tools.role_image',
  speech: 'gui.skillsTools.tools.role_speech',
  video: 'gui.skillsTools.tools.role_video',
  multimodal: 'gui.skillsTools.tools.role_multimodal',
}

const SERVER_WORD: Record<string, string> = {
  off: 'gui.skillsTools.plugins.server_off',
  disconnected: 'gui.skillsTools.plugins.server_idle',
  connecting: 'gui.skillsTools.mcp.connecting',
  connected: 'gui.skillsTools.mcp.connected',
  auth_required: 'gui.skillsTools.mcp.failed',
  error: 'gui.skillsTools.mcp.failed',
}

const roleTitle = (r: Role): string => t(ROLE_NAME[r.id] || 'gui.skillsTools.tools.model')

function ToolGlyph({ id }: { id: string }): JSX.Element {
  if (id === 'web_search' || id === 'web_fetch') return <Icon icon={Globe02Icon} size={20} />
  if (id === 'image_generate') return <Icon icon={Image02Icon} size={20} />
  if (id === 'text_to_speech') return <Icon icon={VolumeHighIcon} size={20} />
  if (id === 'video_generate') return <Icon icon={Video01Icon} size={20} />
  if (id === 'understand_media') return <Icon icon={BrainIcon} size={20} />
  return <Icon icon={Wrench01Icon} size={20} />
}

/* ── Tool configuration modal ──────────────────────────────────── */

function ToolKeyRow({ label, keyName, legacy, url, raw }: {
  label: string
  keyName: string
  legacy?: string | null
  url?: string
  raw: Record<string, unknown>
}): JSX.Element {
  const [value, setValue] = useState('')
  const isSet = !!str(raw, keyName) || (!!legacy && !!str(raw, legacy))
  const busy = useSyncExternalStore(store.subscribe, () => store.isBusy(`set:${keyName}`))

  const save = (): void => {
    if (busy) return
    const v = value.trim()
    if (!v) {
      store.refuse(t('gui.skillsTools.tools.key_first'))
      return
    }
    void store.write(keyName, v).then((ok) => { if (ok) setValue('') })
  }

  const clear = async (): Promise<void> => {
    if (str(raw, keyName)) await store.write(keyName, '')
    if (legacy && str(raw, legacy)) await store.write(legacy, '')
  }

  return (
    <div className="skillsTools-dfield">
      <div className="skillsTools-dfield-label">
        <span>{label}</span>
        <KeyLink url={url} />
      </div>
      <div className="skillsTools-dinput-row">
        <KeyInput
          className="skillsTools-din"
          value={value}
          aria-label={label}
          disabled={busy}
          placeholder={isSet ? t('gui.skillsTools.key_set_ph') : t('gui.skillsTools.key_ph')}
          onChange={(e) => setValue(e.currentTarget.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') save() }}
        />
        <button type="button" className="skillsTools-btn skillsTools-btn-primary" disabled={busy} onClick={save}>
          {busy ? (
            <><span className="skillsTools-spin" aria-hidden="true" />{t('gui.set.checking')}</>
          ) : (
            isSet ? t('gui.skillsTools.update') : t('gui.save')
          )}
        </button>
        {isSet ? (
          <button type="button" className="skillsTools-btn skillsTools-btn-ghost" disabled={busy} onClick={() => void clear()}>
            {t('gui.skillsTools.clear')}
          </button>
        ) : null}
      </div>
    </div>
  )
}

function missingPictureNote(selected: string): string {
  if (!PPT_IMAGE_VENDORS.includes(selected)) return t('gui.skillsTools.tools.ppt_image_missing')
  const name = WEB_VENDOR_LABEL[selected] || selected
  return selected === 'serper'
    ? t('gui.skillsTools.tools.ppt_image_no_key', { name })
    : t('gui.skillsTools.tools.ppt_image_no_key_or_serper', { name })
}

function PictureCallout({ selected }: { selected: string }): JSX.Element | null {
  const picture = useSyncExternalStore(store.subscribe, () => store.get().snap.pictureSearch)
  if (picture.ready && !picture.fallback) return null
  return (
    <div className="skillsTools-callout" data-tone={picture.ready ? 'warn' : 'bad'}>
      <Icon icon={Alert02Icon} size={15} />
      <span>
        {picture.ready
          ? t('gui.skillsTools.tools.ppt_image_fallback', { name: WEB_VENDOR_LABEL[picture.vendor] || picture.vendor })
          : missingPictureNote(selected)}
      </span>
    </div>
  )
}

async function applyRole(r: Role, model: string, provider: string, typed: boolean, kind?: Kind): Promise<SettingsSnapshot | void> {
  const src = store.source()
  if (typed) await src.addModel(provider, model, statedTags(kind ?? 'text'))
  if (r.keys) { await src.set(r.keys[0], model); return src.set(r.keys[1], provider) }
  if (r.everos) return src.everosSet(r.everos, model, provider)
  if (r.media) {
    await src.set(`tools.media.${r.media}`, mediaSelection(store.get().snap.raw, r.media, model, provider))
    return src.set('tools.disabledTools', disabledTools(store.get().snap.raw).filter((x) => x !== r.tool))
  }
  return undefined
}

async function clearRole(r: Role): Promise<SettingsSnapshot | void> {
  const src = store.source()
  if (r.keys) { await src.set(r.keys[0], null); return src.set(r.keys[1], null) }
  if (r.everos) return src.everosSet(r.everos, null)
  if (r.media) {
    await src.set(`tools.media.${r.media}`, mediaSelection(store.get().snap.raw, r.media, ''))
    const dis = disabledTools(store.get().snap.raw)
    return src.set('tools.disabledTools', r.tool && !dis.includes(r.tool) ? [...dis, r.tool] : dis)
  }
  return undefined
}

function ToolRoleField({ role }: { role: Role }): JSX.Element {
  const pill = useRef<HTMLButtonElement>(null)
  const s = store.get()
  const val = roleValue(role, s.snap)
  const provs = roleProviders(role, s.snap)
  const inherit = followsChat(role, s.snap)
  const chat = roleValue(ROLES[0]!, s.snap)

  if (role.everos && s.snap.everos?.available === false && !val) {
    return (
      <div className="skillsTools-dfield">
        <div className="skillsTools-dfield-label"><span>{t('gui.skillsTools.tools.model')}</span></div>
        <span className="skillsTools-mpill" data-dim="true" title={s.snap.everos.note || undefined}>
          <span className="skillsTools-mpill-id">{t('gui.skillsTools.tools.role_everos_missing')}</span>
        </span>
      </div>
    )
  }

  if (!provs.length && !val) {
    const anyOn = s.snap.providers.some((p) => p.on)
    const label = !anyOn
      ? 'gui.skillsTools.tools.role_no_provider'
      : role.media ? 'gui.skillsTools.tools.role_connect_openrouter'
        : role.everos ? 'gui.skillsTools.tools.role_no_vendor' : 'gui.skillsTools.tools.role_no_provider'
    return (
      <div className="skillsTools-dfield">
        <div className="skillsTools-dfield-label"><span>{t('gui.skillsTools.tools.model')}</span></div>
        <span className="skillsTools-mpill" data-dim="true">
          <button
            type="button"
            className="skillsTools-mpill-btn"
            onClick={() => store.openProvider(role.media ? MEDIA_PROVIDER : undefined)}
          >
            <span className="skillsTools-mpill-id">{t(label)}</span>
            <span className="skillsTools-mpill-ch">{'›'}</span>
          </button>
        </span>
      </div>
    )
  }

  const locked = everosLocked(role, s.snap)
  if (locked) {
    const lockKey = locked === 'foreign' ? 'gui.skillsTools.tools.role_locked_foreign' : 'gui.skillsTools.tools.role_locked_env'
    return (
      <div className="skillsTools-dfield">
        <div className="skillsTools-dfield-label"><span>{t('gui.skillsTools.tools.model')}</span></div>
        <span className="skillsTools-mpill" data-dim="true" title={t(lockKey)}>
          <span className="skillsTools-mpill-id">{val ? val.model : t('gui.skillsTools.tools.role_unset')}</span>
          {val ? <span className="skillsTools-mpill-pv">{providerName(s.snap, val.provider)}</span> : null}
        </span>
      </div>
    )
  }

  const required = s.snap.everos?.required || []
  const clearable = !!val && !(role.everos && required.includes(role.everos))
  const offer: Offer = {
    kind: ROLE_KIND[role.id],
    providers: provs.map((p) => p.id),
    title: roleTitle(role),
    current: val ?? (inherit && chat ? chat : undefined),
    pick: async (model, provider, typed, kind) => {
      const before = store.get().snap
      const drawn = withRole(role, before, model, provider)
      store.set({ snap: drawn })
      const ok = await store.run(`role:${role.id}`, () => applyRole(role, model, provider, typed, kind))
      if (!ok && store.get().snap === drawn) store.set({ snap: before })
    },
  }

  return (
    <div className="skillsTools-dfield">
      <div className="skillsTools-dfield-label"><span>{t('gui.skillsTools.tools.model')}</span></div>
      <span className="skillsTools-mpill" data-dim={!val ? 'true' : undefined}>
        <button
          ref={pill}
          type="button"
          className="skillsTools-mpill-btn"
          aria-label={t('gui.skillsTools.tools.role_change', { role: roleTitle(role) })}
          onClick={() => { if (pill.current) openPicker(pill.current, offer) }}
        >
          {val ? (
            <>
              <span className="skillsTools-mpill-id mono">{val.model}</span>
              <span className="skillsTools-mpill-pv">{providerName(s.snap, val.provider)}</span>
            </>
          ) : (
            <span className="skillsTools-mpill-id">
              {inherit ? t('gui.skillsTools.tools.role_follows_chat') : t('gui.skillsTools.tools.role_unset')}
            </span>
          )}
          <span className="skillsTools-mpill-ch">{'⌄'}</span>
        </button>
        {clearable ? (
          <button
            type="button"
            className="skillsTools-mpill-x"
            aria-label={t('gui.skillsTools.tools.role_clear', { role: roleTitle(role) })}
            onClick={() => void store.run(`role:${role.id}`, () => clearRole(role))}
          >
            {'×'}
          </button>
        ) : null}
      </span>
    </div>
  )
}

function ToolModal({ id }: { id: string }): JSX.Element {
  const s = store.get()
  const raw = s.snap.raw
  const row = s.snap.tools.find((r) => r.id === id)
  const disabled = disabledTools(raw)
  const isOn = !disabled.includes(id)
  const blk = isOn ? blocker(id, raw, s.snap) : ''
  const roleId = TOOL_ROLE_OF[id]
  const role = roleId ? ROLES.find((r) => r.id === roleId) : undefined

  const flip = (on: boolean): void => {
    const next = on ? disabled.filter((x) => x !== id) : [...disabled, id]
    void store.write('tools.disabledTools', next, { remount: false })
  }

  const subTone = !isOn ? 'off' : blk ? 'bad' : 'ok'
  const subText = !isOn
    ? t('gui.skillsTools.tools.off')
    : blk === 'key'
      ? t('gui.skillsTools.tools.needs_key')
      : blk === 'model'
        ? t('gui.skillsTools.tools.needs_model')
        : t('gui.skillsTools.tools.ready')

  return (
    <div className="skillsTools-sheet" role="document" aria-label={id}>
      <div className="skillsTools-dglow" aria-hidden="true" />
      <div className="skillsTools-dhead">
        <IconTile tone={toolTone(id)} size="lg">
          <ToolGlyph id={id} />
        </IconTile>
        <div className="skillsTools-dmeta">
          <h3 className="mono">{id}</h3>
          <div className="skillsTools-dsub" data-tone={subTone}>
            <StatusDot tone={subTone} />
            <span>{subText}</span>
          </div>
        </div>
        <Switch on={isOn} label={id} onChange={flip} />
      </div>

      <div className="skillsTools-dbody">
        {row?.one ? <p className="skillsTools-ddesc">{row.one}</p> : null}

        {id === 'web_search' || id === 'web_fetch' ? (() => {
          const pick = WEB_VENDOR[id]!
          const vendor = webVendor(id, raw)
          const keyed = id === 'web_search' || !FETCH_KEYLESS.has(vendor)
          return (
            <>
              <div className="skillsTools-dfield">
                <div className="skillsTools-dfield-label">
                  <span>{t(id === 'web_search' ? 'gui.skillsTools.tools.search_vendor' : 'gui.skillsTools.tools.fetch_vendor')}</span>
                </div>
                <span className="selw skillsTools-selw">
                  <select
                    className="sel"
                    value={vendor}
                    aria-label={t('gui.skillsTools.tools.vendor')}
                    onChange={(e) => void store.write(pick.path, e.currentTarget.value)}
                  >
                    {pick.vendors.map((v) => <option key={v} value={v}>{WEB_VENDOR_LABEL[v] || v}</option>)}
                  </select>
                </span>
              </div>
              {id === 'web_search' ? <PictureCallout selected={vendor} /> : null}
              {keyed || FETCH_KEYLESS.has(vendor) ? (
                <ToolKeyRow
                  label={t('gui.skillsTools.tools.vendor_key', { name: WEB_VENDOR_LABEL[vendor] || vendor })}
                  keyName={vendorKey(vendor)}
                  legacy={legacyKey(id, vendor)}
                  url={WEB_VENDOR_URL[vendor]}
                  raw={raw}
                />
              ) : null}
              {!str(raw, vendorKey(vendor)) && !!borrowedKey(vendor, raw) ? (
                <div className="skillsTools-note">
                  {t('gui.skillsTools.tools.borrowed_key', { name: WEB_VENDOR_LABEL[vendor] || vendor })}
                </div>
              ) : null}
            </>
          )
        })() : null}

        {role ? <ToolRoleField role={role} /> : null}

        <InlineErr text={s.err} />
      </div>

      <div className="skillsTools-dfoot">
        <span className="skillsTools-grow" />
        <button type="button" className="skillsTools-btn" onClick={() => store.closeDetail()}>
          {t('gui.skillsTools.done')}
        </button>
      </div>
    </div>
  )
}

/* ── Skill detail modal ────────────────────────────────────────── */

function SkillInstallLine({ d }: { d: SkillDetail }): JSX.Element | null {
  const inst = d.install as { installed_at?: string | null; version?: string; trigger?: string; score_safety?: number | null } | null | undefined
  if (!inst) return null
  const parts = [
    inst.installed_at ? new Date(inst.installed_at).toLocaleString() : '',
    inst.trigger ? t(TRIGGER[inst.trigger] || 'gui.skillsTools.skills.trig_rpc') : '',
    typeof inst.score_safety === 'number' ? t('gui.skillsTools.skills.safety', { n: inst.score_safety.toFixed(1) }) : '',
  ].filter(Boolean)
  return parts.length ? <div className="skillsTools-skmeta">{parts.join(' · ')}</div> : null
}

function SkillModal({ name, projectDir }: { name: string; projectDir?: string }): JSX.Element {
  const s = store.get()
  const row = projectDir
    ? s.projectInfo?.skills.find((r) => r.name === name)
    : s.snap.skills.find((r) => r.name === name)
  const d = s.skillDetail
  const off = row ? row.blocked : false
  const inst = d && (d.install as { version?: string } | null)

  const flip = (on: boolean): void => {
    void store.toggleSkillBlocked(name, !on)
  }

  const uninstall = (): void => {
    confirm.ask(
      t('gui.skillsTools.skills.uninstall_title', { name }),
      t('gui.skillsTools.skills.uninstall_body', { path: skillDir(d && d.path) || name }),
      t('gui.skillsTools.skills.uninstall'),
      () => {
        void store.run(`uninstall:${name}`, () => store.source().uninstallSkill(name)).then((ok) => {
          if (ok) store.closeDetail()
        })
      },
    )
  }

  const scopeTone = projectDir ? 'teal' : row?.source === 'builtin' ? 'neutral' : 'blue'
  const scopeLabel = projectDir
    ? t('gui.skillsTools.skills.project')
    : row?.source === 'builtin'
      ? t('gui.skillsTools.skills.builtin')
      : t('gui.skillsTools.skills.global')

  return (
    <div className="skillsTools-sheet" role="document" aria-label={name}>
      <div className="skillsTools-dglow" aria-hidden="true" />
      <div className="skillsTools-dhead">
        <IconTile tone={projectDir ? 'teal' : skillTone(name)} size="lg">
          <Icon icon={AiContentGenerator01Icon} size={20} />
        </IconTile>
        <div className="skillsTools-dmeta">
          <h3 className="mono">{String(d?.name || name)}</h3>
          <div className="skillsTools-ctags">
            {row || projectDir ? <Pill tone={scopeTone}>{scopeLabel}</Pill> : null}
            {projectDir ? <Pill tone="mono">{store.folderBaseName(projectDir)}</Pill> : null}
            {inst?.version ? <Pill tone="mono">{inst.version}</Pill> : null}
            {d?.always ? <Pill tone="hub">{t('gui.skillsTools.skills.always')}</Pill> : null}
          </div>
        </div>
        <Switch on={!off} label={name} onChange={flip} />
      </div>

      <div className="skillsTools-dbody">
        {!d ? (
          <div className="skillsTools-mdwait" role="status" aria-busy="true" aria-label={t('gui.skillsTools.loading')}>
            <span className="skillsTools-wbar" style={{ width: '78%', height: '12px' }} />
            <span className="skillsTools-wbar" style={{ width: '56%', height: '12px' }} />
            <span className="skillsTools-wbar" style={{ width: '88%', height: '12px' }} />
            <span className="skillsTools-wbar" style={{ width: '64%', height: '12px' }} />
          </div>
        ) : (
          <>
            {d.description ? <p className="skillsTools-ddesc">{String(d.description)}</p> : null}
            <div className="skillsTools-ftags">
              {((d.files as string[] | undefined) || [MAIN_FILE]).map((f) => (
                <span key={f} className="skillsTools-ftag" data-main={f === MAIN_FILE ? 'true' : undefined}>
                  <span className="mono">{f}</span>
                  <button
                    type="button"
                    className="skillsTools-fbtn"
                    title={t('gui.skillsTools.skills.open_file', { file: f })}
                    aria-label={t('gui.skillsTools.skills.open_file', { file: f })}
                    onClick={() => void store.source().openSkillFile(name, f, projectDir)}
                  >
                    <Icon icon={LinkSquare02Icon} size={12} />
                  </button>
                </span>
              ))}
            </div>
            {projectDir && d.path ? <div className="skillsTools-skmeta mono">{String(d.path)}</div> : null}
            <SkillInstallLine d={d} />
            <div
              className="skillsTools-md prose"
              dangerouslySetInnerHTML={{ __html: md(stripFrontmatter(String(d.body || ''))) }}
            />
          </>
        )}
      </div>

      <div className="skillsTools-dfoot">
        {!projectDir && row?.hub ? (
          <button type="button" className="skillsTools-btn skillsTools-btn-danger" onClick={uninstall}>
            <Icon icon={Delete02Icon} size={14} />
            <span>{t('gui.skillsTools.skills.uninstall')}</span>
          </button>
        ) : projectDir ? (
          <span className="skillsTools-skmeta">{t('gui.skillsTools.skills.project_foot_note')}</span>
        ) : null}
        <span className="skillsTools-grow" />
        <button type="button" className="skillsTools-btn" onClick={() => store.closeDetail()}>
          {t('gui.skillsTools.done')}
        </button>
      </div>
    </div>
  )
}

/* ── MCP server modal ──────────────────────────────────────────── */

const mcpBusyKey = (name: string): string => `mcp:${name}`

function McpKeyField({ m, field }: { m: McpSnapshot; field: AuthField }): JSX.Element {
  const [value, setValue] = useState('')
  const label = field.label || t('gui.skillsTools.mcp.api_key')
  const busy = useSyncExternalStore(store.subscribe, () => store.isBusy(mcpBusyKey(m.name)))
  const save = (): void => {
    if (busy) return
    const v = value.trim()
    if (!v) {
      store.refuse(t('gui.skillsTools.mcp.key_first'))
      return
    }
    void store.run(mcpBusyKey(m.name), () => store.source().configureServer(m.name, { [field.key]: v }))
      .then((ok) => { if (ok) setValue('') })
  }
  const clear = (): void => {
    if (busy) return
    void store.run(mcpBusyKey(m.name), () => store.source().configureServer(m.name, { [field.key]: '' }))
  }
  return (
    <div className="skillsTools-dfield">
      <div className="skillsTools-dfield-label">
        <span>{label}</span>
        <KeyLink url={field.help_url} />
      </div>
      <div className="skillsTools-dinput-row">
        <KeyInput
          className="skillsTools-din"
          value={value}
          aria-label={label}
          disabled={busy}
          placeholder={m.credentialed ? t('gui.skillsTools.key_set_ph') : t('gui.skillsTools.key_ph')}
          onChange={(e) => setValue(e.currentTarget.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') save() }}
        />
        <button type="button" className="skillsTools-btn skillsTools-btn-primary" disabled={busy} onClick={save}>
          {m.credentialed ? t('gui.skillsTools.update') : t('gui.save')}
        </button>
        {m.credentialed ? (
          <button type="button" className="skillsTools-btn skillsTools-btn-ghost" disabled={busy} onClick={clear}>
            {t('gui.skillsTools.clear')}
          </button>
        ) : null}
      </div>
    </div>
  )
}

function McpModal({ name }: { name: string }): JSX.Element {
  const s = store.get()
  const m = s.snap.mcp.find((r) => r.name === name)
  if (!m) return <></>
  const busy = store.isBusy(mcpBusyKey(m.name))
  const kind = mcpChip(m, busy)
  const d = s.mcpDetail

  const flip = (on: boolean): void => {
    void store.run(mcpBusyKey(m.name), () => store.source().toggleServer(m.name, on), { remount: false })
  }
  const auth = (): void => {
    void store.run(mcpBusyKey(m.name), () => store.source().authServer(m.name))
  }
  const revoke = (): void => {
    void store.run(mcpBusyKey(m.name), () => store.source().revokeServer(m.name))
  }
  const retry = (): void => {
    void store.run(mcpBusyKey(m.name), () => store.source().retryServer(m.name))
  }

  const subTone = kind === 'connected' ? 'ok' : kind === 'failed' ? 'bad' : kind === 'setup' || kind === 'connecting' ? 'warn' : 'off'
  const subLabel = kind === 'connected'
    ? t('gui.skillsTools.mcp.connected')
    : kind === 'connecting'
      ? t('gui.skillsTools.mcp.connecting')
      : kind === 'setup'
        ? t('gui.skillsTools.mcp.setup')
        : kind === 'failed'
          ? t('gui.skillsTools.mcp.failed')
          : t('gui.skillsTools.mcp.off')

  return (
    <div className="skillsTools-sheet" role="document" aria-label={m.name}>
      <div className="skillsTools-dglow" aria-hidden="true" />
      <div className="skillsTools-dhead">
        <IconTile tone={mcpTone(m.name)} size="lg">
          <Icon icon={Plug02Icon} size={20} />
        </IconTile>
        <div className="skillsTools-dmeta">
          <h3 className="mono">{m.name}</h3>
          <div className="skillsTools-dsub" data-tone={subTone}>
            {kind === 'connecting' ? <span className="skillsTools-spin" aria-hidden="true" /> : <StatusDot tone={subTone} />}
            <span>{subLabel}</span>
            <Pill tone="mono">{m.transport.toUpperCase()}</Pill>
          </div>
        </div>
        <Switch on={m.enabled} label={m.name} onChange={flip} />
      </div>

      <div className="skillsTools-dbody">
        {!d ? (
          <div className="skillsTools-mdwait" role="status" aria-busy="true" aria-label={t('gui.skillsTools.loading')}>
            <span className="skillsTools-wbar" style={{ width: '44%', height: '12px' }} />
            <span className="skillsTools-wbar" style={{ width: '100%', height: '36px' }} />
          </div>
        ) : (
          <>
            {kind === 'failed' || (busy && m.error) ? (
              <div className="skillsTools-failbox">
                <span className="skillsTools-failtxt mono">{m.error || t('gui.skillsTools.mcp.failed')}</span>
                <button type="button" className="skillsTools-btn" disabled={busy} onClick={retry}>
                  {t(busy ? 'gui.skillsTools.mcp.connecting' : 'gui.skillsTools.mcp.retry')}
                </button>
              </div>
            ) : null}

            {m.auth === 'oauth' ? (
              <div className="skillsTools-oauth-row">
                {m.credentialed ? (
                  <>
                    <Pill tone="ok">{t('gui.skillsTools.mcp.authorized')}</Pill>
                    <button type="button" className="skillsTools-btn skillsTools-btn-ghost" onClick={auth}>
                      {t('gui.skillsTools.mcp.reauth')}
                    </button>
                    <button type="button" className="skillsTools-btn skillsTools-btn-ghost" onClick={revoke}>
                      {t('gui.skillsTools.mcp.revoke')}
                    </button>
                  </>
                ) : (
                  <button type="button" className="skillsTools-btn skillsTools-btn-primary" onClick={auth}>
                    {t('gui.skillsTools.mcp.auth_browser')}
                  </button>
                )}
              </div>
            ) : m.auth === 'none' ? (
              <div className="skillsTools-note">{t('gui.skillsTools.mcp.no_credential')}</div>
            ) : d.fields.length > 0 ? (
              d.fields.map((f) => <McpKeyField key={f.key} m={m} field={f} />)
            ) : (
              <div className="skillsTools-note">
                {t(d.known ? 'gui.skillsTools.mcp.no_credential' : 'gui.skillsTools.mcp.no_fields')}
              </div>
            )}

            <dl className="skillsTools-facts">
              {d.address ? (
                <>
                  <dt>{t('gui.skillsTools.mcp.address')}</dt>
                  <dd className="mono" title={d.address}>{d.address}</dd>
                </>
              ) : null}
              <dt>{t('gui.skillsTools.mcp.tools')}</dt>
              <dd>
                <span>{t('gui.skillsTools.mcp.tools_n', { n: m.tool_count })}</span>
                {d.tools.length > 0 ? (
                  <div className="skillsTools-toolchips">
                    {d.tools.map((tn) => <Pill key={tn} tone="mono">{tn}</Pill>)}
                  </div>
                ) : null}
              </dd>
            </dl>

            <InlineErr text={s.err} />
          </>
        )}
      </div>

      <div className="skillsTools-dfoot">
        <span className="skillsTools-grow" />
        <button type="button" className="skillsTools-btn" onClick={() => store.closeDetail()}>
          {t('gui.skillsTools.done')}
        </button>
      </div>
    </div>
  )
}

/* ── Plugin package modal ──────────────────────────────────────── */

function PluginModal({ name }: { name: string }): JSX.Element {
  const s = store.get()
  const p: AgentPluginRow | undefined = s.pkgs?.packages.find((row) => row.name === name)
  if (!p) return <></>
  const busy = store.isBusy(`pkg:${p.name}`)
  const kind = pkgChip(p)
  const switchable = p.mcp.servers.length > 0 || p.mcp.allowed
  const problems = [...p.notes, ...p.problems, ...p.mcp.problems]

  const flip = (on: boolean): void => {
    void store.run(`pkg:${p.name}`, async () => {
      store.syncPkgs(await store.source().setPackageMcp(p.name, on))
    }, { remount: false })
  }

  return (
    <div className="skillsTools-sheet" role="document" aria-label={p.name}>
      <div className="skillsTools-dglow" aria-hidden="true" />
      <div className="skillsTools-dhead">
        <IconTile tone={pluginTone(p.name)} size="lg">
          <Icon icon={PuzzleIcon} size={20} />
        </IconTile>
        <div className="skillsTools-dmeta">
          <h3 className="mono">{p.name}</h3>
          <div className="skillsTools-ctags">
            <Pill tone="mono">{p.version}</Pill>
            {kind === 'restart' ? <Pill tone="warn">{t('gui.plug.needs_restart')}</Pill> : null}
            {kind === 'problem' ? <Pill tone="bad">{t('gui.skillsTools.plugins.problem')}</Pill> : null}
            {kind === 'unimported' ? <Pill tone="neutral">{t('gui.skillsTools.plugins.unimported')}</Pill> : null}
          </div>
        </div>
      </div>

      <div className="skillsTools-dbody">
        {p.description ? <p className="skillsTools-ddesc">{p.description}</p> : null}

        {problems.map((text) => (
          <div key={text} className="skillsTools-callout" data-tone="bad">
            <Icon icon={Alert02Icon} size={15} />
            <span className="mono">{text}</span>
          </div>
        ))}

        {switchable ? (
          <div className="skillsTools-switch-row">
            <div>
              <div className="skillsTools-switch-label">{t('gui.skillsTools.plugins.mcp_switch')}</div>
              <div className="skillsTools-switch-sub">{t('gui.skillsTools.plugins.mcp_switch_sub')}</div>
            </div>
            <Switch on={p.mcp.allowed} disabled={busy} label={t('gui.skillsTools.plugins.mcp_switch')} onChange={flip} />
          </div>
        ) : null}

        <dl className="skillsTools-facts">
          <dt>{t('gui.skillsTools.plugins.source')}</dt>
          <dd className="mono" title={p.source}>{p.source}</dd>
          <dt>{t('gui.skillsTools.plugins.skills')}</dt>
          <dd>
            <span>{t(p.skills_imported ? 'gui.skillsTools.plugins.skills_in' : 'gui.skillsTools.plugins.skills_out', { n: p.skills.length })}</span>
            {p.skills.length > 0 ? (
              <div className="skillsTools-toolchips">
                {p.skills.map((sn) => <Pill key={sn} tone="mono">{sn}</Pill>)}
              </div>
            ) : null}
          </dd>
          {p.mcp.servers.map((srv) => (
            <div key={srv.name} className="skillsTools-fact-pair">
              <dt>{t('gui.skillsTools.plugins.server')}</dt>
              <dd>
                <span className="mono">{srv.name}</span>
                <span className="skillsTools-srv-st">{t(SERVER_WORD[srv.state] || 'gui.skillsTools.plugins.server_idle')}</span>
                {srv.error ? <span className="skillsTools-srv-err mono">{srv.error}</span> : null}
              </dd>
            </div>
          ))}
        </dl>

        {p.skills.length > 0 && !p.skills_imported ? (
          <div className="skillsTools-pkg-cli">
            <div className="skillsTools-pkg-cli-hint">{t('gui.skillsTools.plugins.import_hint')}</div>
            <CopyCmd command={importCommand(p.source)} />
          </div>
        ) : null}
      </div>

      <div className="skillsTools-dfoot">
        <span className="skillsTools-grow" />
        <button type="button" className="skillsTools-btn" onClick={() => store.closeDetail()}>
          {t('gui.skillsTools.done')}
        </button>
      </div>
    </div>
  )
}

export function DetailModal(): JSX.Element | null {
  const s = store.get()
  const modal = s.modal
  if (!modal) return null
  const content = modal.kind === 'tool'
    ? <ToolModal key={`tool:${modal.id}:${s.epoch}`} id={modal.id} />
    : modal.kind === 'skill'
      ? <SkillModal key={`skill:${modal.name}:${modal.projectDir || ''}`} name={modal.name} projectDir={modal.projectDir} />
      : modal.kind === 'mcp'
        ? <McpModal key={`mcp:${modal.name}`} name={modal.name} />
        : <PluginModal key={`plugin:${modal.name}`} name={modal.name} />
  return createPortal(content, store.detailHost())
}
