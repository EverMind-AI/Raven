import { ds, shell, t } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'

import type { ProviderOp, SettingsSnapshot, SettingsSource, UsageStats } from './types'

/* Page state, outside React on purpose: the legacy shell drives this dialog
 * imperatively (the me button and Cmd+, open it, redrawAll repaints it on a
 * language flip, the capabilities rows repaint it after a credential write),
 * so the state lives in a plain store the shims can call, and the component
 * subscribes.
 *
 * The open tab is NOT here: the chrome jumps the dialog to a section by
 * writing the bare `sTab` global before calling drawSettings(), so that slot
 * stays on window (ui-web/src/demo/130-settings.js declares it) and the store
 * syncs from it on every draw.
 */

declare global {
  interface Window {
    sTab?: string
  }
}

export interface SettingsState {
  tab: string
  snap: SettingsSnapshot
  loaded: boolean
  /* Remounts the whole panel subtree on every draw, so uncontrolled inputs
     restart from the freshly loaded values -- the same full rebuild the
     legacy drawSettings performed with innerHTML. */
  epoch: number
  mdlAdv: boolean
  memEdit: string | null
  /* The tool whose credential editor is unfolded, one at a time. In the store
     rather than the component because every draw remounts the panel. */
  toolKeyEdit: string | null
  /* undefined = never answered (drawn as loading), null = no counter behind
     the page (the demo's no-data note). */
  usage: UsageStats | null | undefined
  provOpen: string | null
  provAll: boolean
  provErr: string
  provBusy: boolean
  provFocus: boolean
}

const initial = (): SettingsState => ({
  tab: 'usage',
  snap: {
    raw: {}, configPath: '~/.raven/config.json', everos: null, providers: [],
    curProvider: '', model: '', toolGroups: [], tools: [],
  },
  loaded: false,
  epoch: 0,
  mdlAdv: false,
  memEdit: null,
  toolKeyEdit: null,
  usage: undefined,
  provOpen: null,
  provAll: false,
  provErr: '',
  provBusy: false,
  provFocus: false,
})

let state: SettingsState = initial()
const listeners = new Set<() => void>()
let lazy = false
let usageBusy = false
let usageAt = 0

export const getState = (): SettingsState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function set(patch: Partial<SettingsState>): void {
  state = { ...state, ...patch }
  for (const l of listeners) l()
}

export const source = (): SettingsSource => ds<SettingsSource>('settings')

const curTab = (): string => (typeof window.sTab === 'string' ? window.sTab : state.tab)

export async function refresh(): Promise<void> {
  set({ loaded: true })
  try {
    const snap = await source().load()
    set({ snap, epoch: state.epoch + 1 })
  } catch (e) {
    toast(`加载失败：${(e as Error).message || String(e)}`)
  }
}

/* The drawSettings shim lands here. The first draw schedules the fixture (or
   rpc) load on a task boundary rather than inline: the boot list draws before
   the live layer has evaluated, and the deferral lets the real source win the
   seam before anything is fetched. */
export function redraw(): void {
  set({ tab: curTab(), epoch: state.epoch + 1 })
  if (!lazy) {
    lazy = true
    setTimeout(() => {
      if (!state.loaded) void refresh()
    }, 0)
  }
}

/* The live layer's openSettings: load, draw, then lift the veil -- the same
   order the legacy open kept, so the dialog never greets with fixture rows. */
export async function open(): Promise<void> {
  lazy = true
  set({ tab: curTab() })
  await refresh()
  shell().openSet?.()
  /* The counters are read when the tab comes up, the way the legacy usage
     page read them on every draw. The poll only keeps them current after
     that, and only while the dialog stays open. */
  if (state.tab === 'usage') void usageLoad()
}

export function setTab(id: string): void {
  window.sTab = id
  set({ tab: id, epoch: state.epoch + 1 })
  if (id === 'usage') void usageLoad()
}

export type WriteOutcome = 'ok' | 'notlive' | 'err'

const isNotLive = (e: unknown): boolean => !!(e as { notLive?: boolean }).notLive

/* One whitelisted dotted key per control. The rpc source reloads and toasts
   on its own; 'notlive' is the fixture's tag, rendered by the caller as the
   in-row refusal so nothing sits between "writes through" and "refuses". */
export async function write(key: string, value: unknown): Promise<WriteOutcome> {
  try {
    const snap = await source().set(key, value)
    set({ snap, epoch: state.epoch + 1 })
    return 'ok'
  } catch (e) {
    if (isNotLive(e)) return 'notlive'
    set({ epoch: state.epoch + 1 })
    return 'err'
  }
}

export async function everosSave(
  section: string,
  fields: Record<string, string> | null,
  borrowFrom?: string,
): Promise<WriteOutcome> {
  try {
    const snap = await source().everosSet(section, fields, borrowFrom)
    set({ snap, memEdit: null, epoch: state.epoch + 1 })
    return 'ok'
  } catch (e) {
    if (isNotLive(e)) return 'notlive'
    set({ epoch: state.epoch + 1 })
    return 'err'
  }
}

export function memEditSet(sec: string): void {
  set({ memEdit: state.memEdit === sec ? null : sec, epoch: state.epoch + 1 })
}

export function toolKeyToggle(id: string): void {
  set({ toolKeyEdit: state.toolKeyEdit === id ? null : id, epoch: state.epoch + 1 })
}

export function advToggle(): void {
  set({ mdlAdv: !state.mdlAdv, epoch: state.epoch + 1 })
}

export function provToggle(id: string): void {
  const open = state.provOpen === id
  set({ provOpen: open ? null : id, provErr: '', provFocus: !open, epoch: state.epoch + 1 })
}

export function provAllToggle(): void {
  set({ provAll: !state.provAll, epoch: state.epoch + 1 })
}

/* Validation refusals land where the legacy provErr did: in the open form. */
export function provSay(msg: string): void {
  set({ provErr: msg, epoch: state.epoch + 1 })
}

export function clearProvFocus(): void {
  state = { ...state, provFocus: false }
}

export async function providerRun(op: ProviderOp, params: Record<string, unknown>): Promise<void> {
  if (state.provBusy) return
  set({ provBusy: true, provErr: '' })
  try {
    const snap = await source().provider(op, params)
    set({ snap, provBusy: false, epoch: state.epoch + 1 })
  } catch (e) {
    const err = e as { data?: { detail?: string }; message?: string }
    const msg = isNotLive(e) ? t('gui.set.not_live') : (err.data && err.data.detail) || err.message || String(e)
    set({ provBusy: false, provErr: msg, epoch: state.epoch + 1 })
  }
}

/* The default-model picker is legacy live chrome; the source hands the
   island a door to it. Returns false when no picker is behind the page. */
export function pickDefault(anchor: HTMLElement): boolean {
  const s = source()
  if (!s.pickModel) return false
  s.pickModel(anchor, () => {
    /* Both halves of the default pair: a cross-provider pick moves the default
       badge with the model, or the page keeps marking the old provider until a
       reload. */
    const src = source()
    set({
      snap: {
        ...state.snap,
        model: src.model(),
        curProvider: src.defaultProvider ? src.defaultProvider() : state.snap.curProvider,
      },
      epoch: state.epoch + 1,
    })
  })
  return true
}

export function checkUpdate(btn: HTMLButtonElement): void {
  void source().checkUpdate(btn)
}

/* Every open re-reads the counters; the floor keeps the redraw-triggered
   re-asks from spinning, and a failed refresh keeps the numbers it already
   has rather than reporting "no usage" for a dropped call. */
export async function usageLoad(): Promise<void> {
  if (usageBusy || Date.now() - usageAt < 3000) return
  usageBusy = true
  let usage = state.usage
  try {
    usage = await source().usage()
  } catch {
    if (usage === undefined) {
      usage = {
        days: 30,
        llm: { total: {
          calls: 0, input_tokens: 0, output_tokens: 0, cost_usd: null,
          cache_read_tokens: null, cache_write_tokens: null, cost_missing_calls: 0,
          cache_read_missing_calls: 0, cache_write_missing_calls: 0, legacy_cost_calls: 0,
        }, models: [] },
        tools: { total: 0, counts: [] },
      }
    }
  }
  usageBusy = false
  usageAt = Date.now()
  set({ usage })
}

/* Test seam: back to the boot state, timers and floors included. */
export function reset(): void {
  state = initial()
  lazy = false
  usageBusy = false
  usageAt = 0
  delete window.sTab
}
