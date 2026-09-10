import { ds, shell, t } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'

import type {
  ModelCandidate,
  ProviderOp,
  SettingsSnapshot,
  SettingsSource,
  UsageStats,
} from './types'

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
  usageSession: string
  usage: UsageStats | null | undefined
  /* The provider the pane is showing, or null before anything is picked --
     the page resolves that to the first connected one rather than storing a
     default, so a refresh that connects a provider moves the pane with it. */
  provOpen: string | null
  /* The drawer over the settings dialog: which provider it is for, and whether
     it is filling a model in by hand or showing what the provider serves.
     Holding the slug rather than a boolean is what lets it name the provider it
     will write to, and survive the redraw a save triggers. */
  drawer: { slug: string; mode: 'add' | 'list' } | null
  addErr: string
  /* The fetched catalogue, kept beside the drawer rather than inside it: a
     redraw must not drop three hundred rows that cost a network round trip. */
  fetch: { busy: boolean; rows: ModelCandidate[]; status: string; error: string }
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
  usageSession: '',
  usage: undefined,
  provOpen: null,
  drawer: null,
  addErr: '',
  fetch: { busy: false, rows: [], status: '', error: '' },
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
  /* The drawer belongs to the model page. Left open across a tab change it
     would come back over whatever section is showing, addressed to a provider
     nobody is looking at any more. */
  set({ tab: id, drawer: null, epoch: state.epoch + 1 })
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

/* Which provider the right-hand pane is showing. A selection, not a toggle:
   the pane is always showing one, so clicking the row you are already on must
   not empty it. */
export function provSelect(id: string): void {
  if (state.provOpen === id) return
  /* No `epoch` bump. That counter remounts the whole panel so uncontrolled
     fields restart from freshly loaded values, and a remounted rail is a new
     element scrolled back to the top -- picking a provider near the bottom of
     forty threw the list back to the first one. Nothing here needs the rebuild:
     the pane is keyed by the provider it shows, so it remounts on its own and
     its fields pick up the new section. */
  set({ provOpen: id, provErr: '', provFocus: true })
}

export function addModelOpen(slug: string): void {
  set({ drawer: { slug, mode: 'add' }, addErr: '' })
}

export function addModelClose(): void {
  if (state.drawer === null) return
  set({ drawer: null, addErr: '' })
}

/* Open the catalogue drawer and ask, in that order: the panel has to be on
   screen while the round trip happens, or a slow provider reads as a button
   that did nothing. */
export async function fetchModelsOpen(slug: string): Promise<void> {
  /* Drawer state only, so no `epoch` bump: the drawer lives outside the keyed
     panel and a rebuild would only cost the rail its scroll position. */
  set({
    drawer: { slug, mode: 'list' },
    addErr: '',
    fetch: { busy: true, rows: [], status: '', error: '' },
  })
  const ask = source().fetchModels
  if (!ask) {
    set({ fetch: { busy: false, rows: [], status: '', error: t('gui.set.not_live') } })
    return
  }
  try {
    const out = await ask(slug)
    /* Dropped if the drawer moved on: a second provider's list must not land
       under the first one's heading. */
    if (state.drawer?.slug !== slug || state.drawer.mode !== 'list') return
    set({ fetch: { busy: false, rows: out.models || [], status: out.status || '', error: out.error || '' } })
  } catch (e) {
    if (state.drawer?.slug !== slug) return
    set({ fetch: { busy: false, rows: [], status: 'error', error: errText(e) } })
  }
}

/* Add or drop one row of the fetched list, and flip that row where it stands.
   The list is the drawer's own state, so a snapshot refresh does not touch it
   -- without this the row a person just added still offers to add it. */
export async function catalogueToggle(slug: string, row: ModelCandidate): Promise<void> {
  const params: Record<string, unknown> = row.added
    ? { slug, model: row.id }
    : {
        slug,
        model: row.id,
        ...(row.label && row.label !== row.id ? { label: row.label } : {}),
        ...(row.capabilities?.length ? { capabilities: row.capabilities } : {}),
        ...(row.input_modalities?.length ? { input_modalities: row.input_modalities } : {}),
        ...(row.output_modalities?.length ? { output_modalities: row.output_modalities } : {}),
      }
  const before = row.added
  await providerRun(before ? 'remove_model' : 'add_model', params)
  if (state.provErr) return
  set({
    fetch: {
      ...state.fetch,
      rows: state.fetch.rows.map((r) => (r.id === row.id ? { ...r, added: !before } : r)),
    },
  })
}

/* Every row the list is currently showing, one call at a time. Sequential
   because each write rewrites the config file: fired together they race, and
   the last writer wins with a list missing everything the others added. */
export async function catalogueAddAll(slug: string, rows: ModelCandidate[]): Promise<void> {
  for (const row of rows) {
    if (row.added) continue
    await catalogueToggle(slug, row)
    if (state.provErr) return
  }
}

/* Add a model with what the person stated about it, and shut the drawer only
   if it lands. A refusal -- a duplicate id, a provider that went away -- has to
   stay in front of the form that caused it, with the fields still filled. */
export async function addModelSave(params: Record<string, unknown>): Promise<void> {
  if (state.provBusy) return
  set({ provBusy: true, addErr: '' })
  try {
    const snap = await source().provider('add_model', params)
    set({ snap, provBusy: false, drawer: null, addErr: '', epoch: state.epoch + 1 })
  } catch (e) {
    const msg = errText(e)
    set({ provBusy: false, addErr: msg, epoch: state.epoch + 1 })
  }
}

/* Validation refusals land where the legacy provErr did: in the open form. */
export function provSay(msg: string): void {
  set({ provErr: msg, epoch: state.epoch + 1 })
}

export function clearProvFocus(): void {
  state = { ...state, provFocus: false }
}

/* What a refused provider write says to the reader. Shared by the two callers
   so a demo-mode refusal reads the same wherever it lands. */
function errText(e: unknown): string {
  const err = e as { data?: { detail?: string }; message?: string }
  return isNotLive(e) ? t('gui.set.not_live') : (err.data && err.data.detail) || err.message || String(e)
}

export async function providerRun(op: ProviderOp, params: Record<string, unknown>): Promise<void> {
  if (state.provBusy) return
  set({ provBusy: true, provErr: '' })
  try {
    const snap = await source().provider(op, params)
    set({ snap, provBusy: false, epoch: state.epoch + 1 })
  } catch (e) {
    set({ provBusy: false, provErr: errText(e), epoch: state.epoch + 1 })
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
export function usageSelect(session: string): void {
  set({ usageSession: session, usage: undefined })
  usageAt = 0
  void usageLoad()
}

export async function usageLoad(): Promise<void> {
  if (usageBusy || Date.now() - usageAt < 3000) return
  usageBusy = true
  const selected = state.usageSession
  let usage = state.usage
  try {
    usage = await source().usage(selected || undefined)
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
  if (selected !== state.usageSession) { void usageLoad(); return }
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
