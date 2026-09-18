/* The settings dialog's state and verbs.
 *
 * Page state, outside React on purpose: three of the callers that drive this
 * dialog are not React. The platform's settings shortcut opens it
 * (state/globalListeners.ts), the whole-page language repaint bumps its epoch
 * (state/lang/effects.ts) and the boot repaints it once the gateway answers
 * (app/boot.ts) -- so the state lives in a plain store those three can call,
 * and the component subscribes.
 *
 * The open section is synced from the shared slot (state/settings.ts) on
 * every draw: the chrome jumps the dialog to a section by writing that slot
 * before asking for the repaint.
 */
import { t } from '../../i18n/t'
import { settingsTab } from '../../state/settings'
import * as settingsDialog from '../../state/settings'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'

import type {
  ArchivedSession,
  ModelCandidate,
  SettingsSnapshot,
  SettingsSource,
  SkillDetail,
  UsageRange,
  UsageStats,
} from './types'

export type SectionId = 'general' | 'usage' | 'model' | 'skills' | 'tools' | 'plugins' | 'archive' | 'about'
export const SECTIONS: SectionId[] = ['general', 'usage', 'model', 'skills', 'tools', 'plugins', 'archive', 'about']

/* The vendor-list sheet under a provider's models card: what the vendor
   answered, what is ticked, and the typed filter. Kept here rather than in
   the component because every write remounts the page. */
export interface Sheet {
  slug: string
  q: string
  sel: string[]
  state: 'loading' | 'ready' | 'failed'
  items: ModelCandidate[]
}

/* A device flow in progress: the code the vendor's page asks for, and when
   it stops being valid. */
export interface Oauth {
  slug: string
  uri: string
  code: string
  until: number
  expired: boolean
}

export interface SettingsState {
  tab: SectionId
  snap: SettingsSnapshot
  loaded: boolean
  /* Remounts the page subtree on every draw, so uncontrolled inputs restart
     from the freshly loaded values. */
  epoch: number
  /* The page-side refusal under the current page, or ''. */
  err: string
  /* Writes in flight, by a key the row chooses; drawn as "connecting". */
  busy: string[]
  range: UsageRange & { kind: string }
  /* undefined = never answered (drawn as loading), null = the counter did not
     answer. */
  usage: UsageStats | null | undefined
  /* The provider whose detail is open, or null for the list. */
  provider: string | null
  /* The slug picked in the add-provider block, or null when it is closed. */
  provAdd: string | null
  sheet: Sheet | null
  hdrAdd: string | null
  ovlAdd: string | null
  chatCfg: boolean
  /* The role whose model picker is open, or null. */
  picker: string | null
  oauth: Oauth | null
  skill: string | null
  detail: SkillDetail | null
  skq: string
  toolOpen: string | null
  plugOpen: string | null
  archived: ArchivedSession[] | null
}

const emptySnap = (): SettingsSnapshot => ({
  raw: {}, configPath: '~/.raven/config.json', everos: null, providers: [],
  curProvider: '', model: '', tools: [], skills: [], mcp: [],
})

const initial = (): SettingsState => ({
  tab: 'general',
  snap: emptySnap(),
  loaded: false,
  epoch: 0,
  err: '',
  busy: [],
  range: { kind: '30', ...lastDays(30) },
  usage: undefined,
  provider: null,
  provAdd: null,
  sheet: null,
  hdrAdd: null,
  ovlAdd: null,
  chatCfg: false,
  picker: null,
  oauth: null,
  skill: null,
  detail: null,
  skq: '',
  toolOpen: null,
  plugOpen: null,
  archived: null,
})

const store = makeStore<SettingsState>(initial())
let lazy = false
let oauthTimer: ReturnType<typeof setInterval> | null = null

export const { get, subscribe } = store

/** A patch, merged into the page's state. */
export function set(patch: Partial<SettingsState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

export const source = (): SettingsSource => ds('settings')

/* ISO day, local time: the range the usage page asks for is the reader's
   calendar, and the server reads the same wall clock. */
export function isoDay(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

/** Today and the n-1 days before it. */
export function lastDays(n: number): UsageRange {
  const to = new Date()
  const from = new Date(to)
  from.setDate(to.getDate() - (n - 1))
  return { from: isoDay(from), to: isoDay(to) }
}

const curTab = (): SectionId => {
  const id = settingsTab.id ?? get().tab
  return (SECTIONS as string[]).includes(id) ? (id as SectionId) : 'general'
}

/* A reload coalesced over a burst of pushes -- an MCP sync reports one status
   per server -- and skipped while the dialog is down: nothing is looking. */
let refreshTimer: ReturnType<typeof setTimeout> | null = null
export const REFRESH_SOON_MS = 300
export function refreshSoon(): void {
  if (!settingsDialog.isOpen() || !get().loaded) return
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => { refreshTimer = null; void refresh() }, REFRESH_SOON_MS)
}

export async function refresh(): Promise<void> {
  set({ loaded: true })
  try {
    const snap = await source().load()
    set({ snap, epoch: get().epoch + 1 })
  } catch (e) {
    toast(t('gui.op.load_failed', { detail: (e as Error).message || String(e) }))
  }
}

/* Where the boot's own draw step and the language repaint land (app/boot.ts,
   state/lang/effects.ts). The first draw schedules the load on a task boundary
   rather than inline: the boot list draws before every source is on the seam,
   and the deferral lets the real one win it before anything is fetched. */
export function redraw(): void {
  set({ tab: curTab(), epoch: get().epoch + 1 })
  if (!lazy) {
    lazy = true
    setTimeout(() => {
      if (!get().loaded) void refresh()
    }, 0)
  }
}

/* Load, draw, then lift the veil, in that order, so the dialog never greets
   with stale rows. */
export async function open(): Promise<void> {
  await refresh()
  redraw()
  settingsDialog.open()
}

export async function openModels(): Promise<void> {
  settingsTab.id = 'model'
  await open()
}

export async function openProviderModels(slug: string): Promise<void> {
  settingsTab.id = 'model'
  set({ provider: slug })
  await open()
}

/* A section pick closes every drawer of the section it leaves. */
export function setTab(id: string): void {
  settingsTab.id = id
  set({
    tab: curTab(), err: '', provider: null, provAdd: null, sheet: null, hdrAdd: null, ovlAdd: null,
    picker: null, skill: null, detail: null, toolOpen: null, plugOpen: null,
  })
}

/* Every write the pages make: the row's key is busy while it runs, a fresh
   snapshot lands when it resolves, and a refusal the source already toasted
   (its { handled } tag) only redraws. Resolves to whether it went through. */
export async function run(key: string, work: () => Promise<SettingsSnapshot | void>): Promise<boolean> {
  set({ busy: [...get().busy, key], err: '' })
  try {
    const snap = await work()
    if (snap) set({ snap, epoch: get().epoch + 1 })
    return true
  } catch (e) {
    if (!(e as { handled?: boolean }).handled) {
      toast(t('gui.plug.op_failed', { err: (e as Error).message || String(e) }))
    }
    return false
  } finally {
    set({ busy: get().busy.filter((k) => k !== key) })
  }
}

export const write = (key: string, value: unknown): Promise<boolean> =>
  run(`set:${key}`, () => source().set(key, value))

export const isBusy = (key: string): boolean => get().busy.includes(key)

/* A page-side refusal: nothing is written, the page says why. */
export function refuse(msg: string): void {
  set({ err: msg })
}

export async function usageLoad(range: UsageRange & { kind: string }): Promise<void> {
  set({ range, usage: undefined })
  let u: UsageStats | null = null
  try {
    u = await source().usage(range)
  } catch (e) {
    toast(t('gui.op.load_failed', { detail: (e as Error).message || String(e) }))
  }
  if (get().range === range) set({ usage: u })
}

export async function archivedLoad(): Promise<void> {
  try {
    set({ archived: await source().archived() })
  } catch (e) {
    toast(t('gui.op.load_failed', { detail: (e as Error).message || String(e) }))
    set({ archived: [] })
  }
}

export async function skillOpen(name: string): Promise<void> {
  set({ skill: name, detail: null })
  try {
    const detail = await source().inspectSkill(name)
    if (get().skill === name) set({ detail })
  } catch {
    /* toasted by the source; the detail stays on its loading note */
  }
}

/* Open the vendor-list sheet for a provider and ask what it serves. A vendor
   with no list endpoint answers a status other than ok, and the sheet then
   takes a typed id alone. */
export async function sheetOpen(slug: string): Promise<void> {
  set({ sheet: { slug, q: '', sel: [], state: 'loading', items: [] } })
  let items: ModelCandidate[] = []
  let ok = false
  try {
    const r = await source().fetchModels(slug)
    items = r.models || []
    ok = r.status === 'ok'
  } catch {
    ok = false
  }
  const sheet = get().sheet
  if (sheet && sheet.slug === slug) set({ sheet: { ...sheet, state: ok ? 'ready' : 'failed', items } })
}

export function sheetPatch(patch: Partial<Sheet>): void {
  const sheet = get().sheet
  if (sheet) set({ sheet: { ...sheet, ...patch } })
}

export function sheetToggle(id: string): void {
  const sheet = get().sheet
  if (!sheet) return
  const sel = sheet.sel.includes(id) ? sheet.sel.filter((m) => m !== id) : [...sheet.sel, id]
  set({ sheet: { ...sheet, sel } })
}

/* Start a device flow and watch for it to land: the provider turns connected
   on a later `model.options`, so the page polls that read until it does or the
   code expires. */
export async function oauthStart(slug: string): Promise<void> {
  oauthStop()
  let r
  try {
    r = await source().oauthLogin(slug)
  } catch {
    return
  }
  const until = Date.now() + Math.max(30, r.expires_in) * 1000
  set({ oauth: { slug, uri: r.verification_uri, code: r.user_code, until, expired: false } })
  oauthTimer = setInterval(() => { void oauthPoll() }, OAUTH_POLL_MS)
}

export const OAUTH_POLL_MS = 3000

async function oauthPoll(): Promise<void> {
  const o = get().oauth
  if (!o) { oauthStop(); return }
  await refresh()
  const p = get().snap.providers.find((x) => x.id === o.slug)
  if (p && p.on) { oauthStop(); set({ oauth: null }); return }
  if (Date.now() > o.until) { oauthStop(); set({ oauth: { ...o, expired: true } }) }
}

function oauthStop(): void {
  if (oauthTimer) clearInterval(oauthTimer)
  oauthTimer = null
}

export function _resetForTests(): void {
  oauthStop()
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = null
  store._resetForTests()
  store.set(initial())
  lazy = false
}
