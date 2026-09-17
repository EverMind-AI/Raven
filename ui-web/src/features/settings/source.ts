/* -- settings: the rpc source -----------------------------------------
   The settings island (ui-web/src/features/settings/) owns the dialog's
   drawing; this module speaks settings.* over /rpc and holds what one read
   fills -- the raw config, where it is on disk, and the EverOS sections. The
   model half is beside it in features/model/source.ts, which this one asks for
   the provider list and the default pair.

   The permission chip's refresh is here too: it mirrors what the gate reads
   for the visible conversation, which is a settings question rather than the
   chip's own. */

import type { ProviderOp, SettingsSnapshot, SettingsSource, ToolGroup } from './types'
import type { ParamsOf, ResultOf } from '../../rpc/generated'
import type { BannerSource } from '../../state/banner'

import { defaultModel, defaultProvider, loadProviders, providers, setDefaultPair, showModel } from '../model/source'
import { open as openModelPicker } from '../model/store'
import { extTools, loadExt } from '../plugins/source'
import { draw as drawBanner } from '../../state/banner'
import { t } from '../../i18n/t'
import { setFromConfig as setPermMode } from '../../state/perm'
import { current as sessionCurrent } from '../../lib/session'
import { show as toast } from '../../state/toast'
import { gateway } from '../../rpc/gateway'
import { generation } from '../../state/session/generation'
import { staging } from '../../state/session/staging'

/* The groups the dialog draws its tool rows under, and their order. Page data
   rather than a fixture: both modes draw the same four, and which one a tool
   falls into is decided from its name (features/plugins/source.ts). */
export const TOOL_GROUPS: ToolGroup[] = [
  { id: 'file', label: 'gui.toolgrp.file', hint: 'gui.toolgrp.file_hint' },
  { id: 'run', label: 'gui.toolgrp.run' },
  { id: 'net', label: 'gui.toolgrp.net' },
  { id: 'ask', label: 'gui.toolgrp.ask' },
]

/* The config settings.get returned. Keys arrive camelCased
   (agents.defaults.reasoningEffort), one level per dot. Handed to the island
   inside its snapshot, which is the only reader. */
let RAW: Record<string, unknown> = {}

let configPathLive = '~/.raven/config.json'
let everosLive: ResultOf<'settings.everos'> | null = null

/* The three members that need page chrome no island owns: the version the foot
   learned from `system.version`, the check that drives the update notice and
   the upgrade prompt, and the language pick. Installed by
   features/settings/chrome.ts, which owns the settings transport. */
export interface SettingsChrome {
  version(): string | null
  checkUpdate(btn: HTMLButtonElement): void | Promise<void>
  setLang(v: string): void
}

let chrome: SettingsChrome = {
  version: () => null,
  checkUpdate: () => {},
  setLang: () => {},
}

export function setSettingsChrome(next: SettingsChrome): void {
  chrome = next
}

/* Read by the send path, next to the staged model and tier. The object is
   reset on both paths that abandon a draft. */
export function stagedPerm(): string | null {
  const s = staging()
  const mode = s.perm
  s.perm = null
  return mode
}

export async function loadEveros(): Promise<void> {
  try {
    everosLive = await gateway().call('settings.everos', {})
  } catch { /* section rows render as unset; writes still surface their error */ }
}

/* The permission chip mirrors what the gate reads for the visible conversation:
   its own mode when it has one, else the default. Asked of the server rather
   than lifted from the settings snapshot, which only knows the default; pushed
   into the island so the chip needs no transport of its own. */
export async function loadPermMode(sid?: string | null, gen?: number): Promise<void> {
  // The same ticket loadProviders carries, for the same race: two opens in a
  // row, the first answer landing last and repainting the one chip with the
  // mode of a conversation the reader has left.
  const ticket = gen !== undefined ? gen : generation()
  let r
  try {
    r = await gateway().call('config.get', { keys: ['permissions.mode'], ...(sid ? { session_id: sid } : {}) })
  } catch {
    return
  }
  if (ticket !== generation()) return
  setPermMode(String(((r && r.config) || {})['permissions.mode'] || 'ask'))
}

export const pushPermMode = (): Promise<void> => loadPermMode(sessionCurrent())

export async function loadSettings(): Promise<void> {
  const r = await gateway().call('settings.get', {})
  RAW = (r.settings || {}) as Record<string, unknown>
  configPathLive = r.config_path || configPathLive
  drawBanner()
  const agents = RAW.agents as { defaults?: { model?: string; provider?: string } } | undefined
  const defaults = (agents && agents.defaults) || {}
  // The configured default, kept apart from the visible session's model: the
  // settings default-model control shows and edits THIS pair (model AND
  // provider), while the composer chip shows whatever the open conversation
  // runs. Sharing one value made the settings control display the session's
  // model -- and badge the session's provider -- as the default.
  setDefaultPair(defaults.model || '', defaults.provider || '')
  if (defaults.model) showModel(defaults.model)
  try { await loadProviders() } catch { /* model options unavailable — keep the rows already shown */ }
}

export const settingsSnapshot = (): SettingsSnapshot => ({
  raw: RAW, configPath: configPathLive, everos: everosLive,
  // Both default-scoped on purpose: the settings page describes what new
  // conversations start on, so pairing the default model with the visible
  // session's provider badged the wrong row whenever the two scopes differ.
  providers: providers(), curProvider: defaultProvider(), model: defaultModel(),
  toolGroups: TOOL_GROUPS, tools: extTools(),
}) as SettingsSnapshot

export const settingsErr = (e: unknown): string => {
  const err = e as { data?: { detail?: string }; message?: string }
  return (err && err.data && err.data.detail) || (err && err.message) || String(e)
}

/* The banner's draw is the shell's; what it draws for is this page's business.
   No websearch notice in live mode: a config gap belongs in the settings page,
   not as a strip above every conversation. Said through the source rather than
   by replacing drawBanner, which is what it used to do -- and replacing the
   drawing suppressed the OTHER notice too. A memory fault is not a config gap:
   it means the backend has stopped storing and has been handing back
   normal-looking replies the whole time, and live mode is the only mode where
   it can happen at all. Refusing one notice is a decision about that notice. */
export const bannerSource: BannerSource = {
  websearchNeeds: () => false,
}

export const settingsSource: SettingsSource = {
  load: async () => {
    /* The tool inventory is part of settings. Loading it here keeps every
       opener on the island's one refresh path rather than replacing the
       demo-layer openSettings binding in live mode. */
    try { await loadExt() } catch (e) { toast(t('gui.op.load_failed', { detail: settingsErr(e) })) }
    await loadSettings()
    void pushPermMode()
    await loadEveros()
    return settingsSnapshot()
  },
  /* Toasts are spoken here, where the wording lives; the thrown handled tag
     tells the island to only redraw. */
  set: async (key, value) => {
    try {
      await gateway().call('settings.set', { key, value: value as ParamsOf<'settings.set'>['value'] })
      await loadSettings()
      void pushPermMode()
      toast(t('gui.set.saved'))
    } catch (e) {
      toast(t('gui.plug.op_failed', { err: settingsErr(e) }))
      throw { handled: true }
    }
    return settingsSnapshot()
  },
  /* A null fields object means "clear the section" (optional roles only). */
  everosSet: async (section, fields, borrowFrom) => {
    const p: ParamsOf<'settings.everosSet'> = fields ? { section, fields } : { section, clear: true }
    /* Only the name travels. The key stays where it is and the server copies
       it across -- what this page holds is `****set****`. */
    if (borrowFrom) p.borrow_from = borrowFrom
    try {
      await gateway().call('settings.everosSet', p)
      await loadEveros()
      toast(t('gui.set.mem.saved'))
    } catch (e) {
      toast(t('gui.plug.op_failed', { err: settingsErr(e) }))
      throw { handled: true }
    }
    return settingsSnapshot()
  },
  usage: (sessionKey) => gateway().call('settings.usage', { session_key: sessionKey || null }),
  /* The four writes spelled out rather than built as `'model.' + op`: a
     composed name is a string nothing can check, and these are the four the
     pane offers (`ProviderOp` in features/settings/types.ts declares the
     same set). */
  provider: async (op: ProviderOp, params) => {
    /* The island builds the params, and its own interface types them as a bag
       -- one signature for four writes whose fields differ. The name is the
       part that has to be checkable, and spelling the four out is what makes
       it so. */
    if (op === 'save_key') await gateway().call('model.save_key', params as unknown as ParamsOf<'model.save_key'>)
    else if (op === 'add_model') await gateway().call('model.add_model', params as unknown as ParamsOf<'model.add_model'>)
    else if (op === 'remove_model') await gateway().call('model.remove_model', params as unknown as ParamsOf<'model.remove_model'>)
    else if (op === 'disconnect') await gateway().call('model.disconnect', params as unknown as ParamsOf<'model.disconnect'>)
    else throw new Error(`no provider op ${String(op)}`)
    await loadProviders()
    return settingsSnapshot()
  },
  /* A read, so no reload after it: the fetched list is the drawer's own state
     and the page behind it has not changed. Adding a row from that list goes
     through `provider` above, which does refresh. */
  fetchModels: (slug) => gateway().call('model.fetch_models', { slug }),
  model: () => defaultModel(),
  defaultProvider: () => defaultProvider(),
  version: () => chrome.version(),
  checkUpdate: (btn) => chrome.checkUpdate(btn),
  /* The composer's picker popover, offered to the island's default-model
     button so both places pick a model the same way. */
  pickModel: (anchor, after) => openModelPicker(anchor, after, defaultModel()),
  /* Not awaited: the pick repaints synchronously and the persist speaks for
     itself if it fails. */
  setLang: (v) => chrome.setLang(v),
}
