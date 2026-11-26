/* State and actions for the Skills & Tools module page.
 *
 * Holds the active capability tab, filter/search controls, cached snapshot,
 * agent-plugin packages, and the centered `#detail` modal state. Uses makeStore
 * so React subscriptions stay synchronous and outside callers (rail, reconnect,
 * MCP status pushes, shared detail close) share one source of truth.
 */
import { t } from '../../i18n/t'
import * as detail from '../../state/detail'
import * as page from '../../state/page'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'

import type {
  AgentPluginList,
  DetailTarget,
  McpDetail,
  SettingsSnapshot,
  SkillDetail,
  SkillScopeFilter,
  SkillsToolsSource,
  SkillsToolsTab,
} from './types'

export const TABS: readonly SkillsToolsTab[] = ['skills', 'tools', 'mcp', 'plugins']

export interface SkillsToolsState {
  tab: SkillsToolsTab
  snap: SettingsSnapshot
  loaded: boolean
  epoch: number
  err: string
  busy: string[]
  skq: string
  skillFilter: SkillScopeFilter
  toolGroup: string
  toolQ: string
  modal: DetailTarget | null
  skillDetail: SkillDetail | null
  mcpDetail: McpDetail | null
  pkgs: AgentPluginList | null
}

const emptySnap = (): SettingsSnapshot => ({
  raw: {},
  configPath: '~/.raven/config.json',
  skillHub: { on: false },
  pictureSearch: { vendor: '', ready: false, fallback: false },
  everos: null,
  everosCloud: null,
  providers: [],
  curProvider: '',
  model: '',
  tools: [],
  skills: [],
  mcp: [],
})

const initial = (): SkillsToolsState => ({
  tab: 'skills',
  snap: emptySnap(),
  loaded: false,
  epoch: 0,
  err: '',
  busy: [],
  skq: '',
  skillFilter: 'all',
  toolGroup: 'all',
  toolQ: '',
  modal: null,
  skillDetail: null,
  mcpDetail: null,
  pkgs: null,
})

const store = makeStore<SkillsToolsState>(initial())

export const { get, subscribe } = store

export function set(patch: Partial<SkillsToolsState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

export const source = (): SkillsToolsSource => ds('skillsTools')

type AskInChat = (text: string) => void
let askInChat: AskInChat | null = null

export function lendAskInChat(fn: AskInChat | null): void {
  askInChat = fn
}

export function addInChat(text: string): void {
  askInChat?.(text)
}

type OpenProvider = (slug?: string) => void
let openProviderFn: OpenProvider | null = null

export function lendOpenProvider(fn: OpenProvider | null): void {
  openProviderFn = fn
}

export function openProvider(slug?: string): void {
  openProviderFn?.(slug)
}

const sameSnap = (a: SettingsSnapshot, b: SettingsSnapshot): boolean => {
  try {
    return JSON.stringify(a) === JSON.stringify(b)
  } catch {
    return false
  }
}

let loading: Promise<void> | null = null

export async function refresh(): Promise<void> {
  if (loading) return loading
  loading = (async () => {
    try {
      const snap = await source().load()
      const cur = get()
      if (cur.loaded && sameSnap(cur.snap, snap)) return
      set({ snap, loaded: true, epoch: cur.epoch + 1 })
    } catch (e) {
      toast(t('gui.op.load_failed', { detail: (e as Error).message || String(e) }))
    } finally {
      loading = null
    }
  })()
  return loading
}

export function syncSnap(snap: SettingsSnapshot): void {
  const cur = get()
  if (!cur.loaded || sameSnap(cur.snap, snap)) return
  set({ snap, epoch: cur.epoch + 1 })
}

let refreshTimer: ReturnType<typeof setTimeout> | null = null
export const REFRESH_SOON_MS = 300

export function refreshSoon(): void {
  if (page.get() !== 'skillsToolsPage' || !get().loaded) return
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    void refresh()
  }, REFRESH_SOON_MS)
}

export async function open(tab?: SkillsToolsTab): Promise<void> {
  if (tab) set({ tab, err: '' })
  page.show('skillsToolsPage')
  await Promise.all([refresh(), pkgsLoad()])
}

export function close(): void {
  page.show(null)
}

export function setTab(tab: SkillsToolsTab): void {
  if (get().modal) detail.close()
  set({ tab, err: '' })
  if (tab === 'plugins' && get().pkgs === null) void pkgsLoad()
}

export function detailHost(): HTMLDivElement {
  return detail.host('skillsTools')
}

export async function openSkill(name: string): Promise<void> {
  set({ modal: { kind: 'skill', name }, skillDetail: null, err: '' })
  detail.open('skillsTools', { fill: true })
  try {
    const info = await source().inspectSkill(name)
    const cur = get().modal
    if (cur?.kind === 'skill' && cur.name === name) set({ skillDetail: info })
  } catch {
    /* toasted by the source; modal stays on its skeleton */
  }
}

export function openTool(id: string): void {
  set({ modal: { kind: 'tool', id }, err: '' })
  detail.open('skillsTools')
}

export async function openMcp(name: string): Promise<void> {
  set({ modal: { kind: 'mcp', name }, mcpDetail: null, err: '' })
  detail.open('skillsTools', { fill: true })
  try {
    const info = await source().serverDetail(name)
    const cur = get().modal
    if (cur?.kind === 'mcp' && cur.name === name) set({ mcpDetail: info })
  } catch {
    const cur = get().modal
    if (cur?.kind === 'mcp' && cur.name === name) set({ mcpDetail: { known: false, fields: [], tools: [] } })
  }
}

export function openPlugin(name: string): void {
  set({ modal: { kind: 'plugin', name }, err: '' })
  detail.open('skillsTools')
}

export function closeDetail(): void {
  detail.close()
}

function dismissed(): void {
  if (!get().modal) return
  const gen = detail.get().gen
  detail.dropAfterFade(
    () => set({ modal: null, skillDetail: null, mcpDetail: null, err: '' }),
    () => detail.get().gen !== gen,
  )
}
detail.onClose('skillsTools', dismissed)

export async function run(
  key: string,
  work: () => Promise<SettingsSnapshot | void>,
  opts?: { remount?: boolean },
): Promise<boolean> {
  set({ busy: [...get().busy, key], err: '' })
  try {
    const snap = await work()
    if (snap) set({ snap, epoch: opts?.remount === false ? get().epoch : get().epoch + 1 })
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

export const write = (key: string, value: unknown, opts?: { remount?: boolean }): Promise<boolean> =>
  run(`set:${key}`, () => source().set(key, value), opts)

export const isBusy = (key: string): boolean => get().busy.includes(key)

export function refuse(msg: string): void {
  set({ err: msg })
}

let pkgsLoading: Promise<void> | null = null
let pkgsEpoch = 0

/* A write's answer is newer than any package read already in flight. */
export function syncPkgs(pkgs: AgentPluginList): void {
  pkgsEpoch += 1
  set({ pkgs })
}

export function pkgsLoad(): Promise<void> {
  if (pkgsLoading) return pkgsLoading
  const epoch = ++pkgsEpoch
  const loading = (async () => {
    try {
      const pkgs = await source().agentPlugins()
      if (epoch === pkgsEpoch) set({ pkgs })
    } catch (e) {
      if (epoch === pkgsEpoch) {
        toast(t('gui.op.load_failed', { detail: (e as Error).message || String(e) }))
        set({ pkgs: { packages: [], problems: [] } })
      }
    }
  })()
  pkgsLoading = loading
  void loading.then(() => {
    if (pkgsLoading === loading) pkgsLoading = null
  })
  return loading
}

export function _resetForTests(): void {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = null
  loading = null
  pkgsLoading = null
  pkgsEpoch += 1
  askInChat = null
  openProviderFn = null
  store._resetForTests()
  store.set(initial())
}
