/* State and actions for the Skills & Tools module page.
 *
 * Holds the active capability tab, filter/search controls, cached snapshot,
 * project-scoped skills discovery state and directory picker, agent-plugin
 * packages, and the centered `#detail` modal state. Uses makeStore so React
 * subscriptions stay synchronous and outside callers share one source of truth.
 */
import { t } from '../../i18n/t'
import { current as sessionCurrent } from '../../lib/session'
import { servesFolderDialog } from '../../rpc/capabilities'
import * as detail from '../../state/detail'
import * as page from '../../state/page'
import { rows as sessionRows, sess } from '../../state/session/rows'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'

import type {
  AgentPluginList,
  DetailTarget,
  DirListing,
  McpDetail,
  ProjectSkillsInfo,
  SettingsSnapshot,
  SkillDetail,
  SkillScopeFilter,
  SkillsToolsSource,
  SkillsToolsTab,
} from './types'

export const TABS: readonly SkillsToolsTab[] = ['skills', 'tools', 'mcp', 'plugins']

const PROJECT_DIR_STORAGE = 'raven.skillsTools.projectDir'
const RECENT_PROJECTS_MAX = 6

export interface RecentProjectFolder {
  path: string
  name: string
  current: boolean
}

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
  projectDir: string | null
  projectInfo: ProjectSkillsInfo | null
  projectLoading: boolean
  projectErr: string
  pickerOpen: boolean
  pickerView: 'menu' | 'browse' | 'drives'
  pickerListing: DirListing | null
  pickerLoading: boolean
  pickerPicking: boolean
  pickerAsked: number
  pickerErr: string
}

const emptySnap = (): SettingsSnapshot => ({
  raw: {},
  configPath: '~/.raven/config.json',
  skillHub: { on: false },
  projectSkillsOn: true,
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
  projectDir: null,
  projectInfo: null,
  projectLoading: false,
  projectErr: '',
  pickerOpen: false,
  pickerView: 'menu',
  pickerListing: null,
  pickerLoading: false,
  pickerPicking: false,
  pickerAsked: 0,
  pickerErr: '',
})

const store = makeStore<SkillsToolsState>(initial())

export const { get, subscribe } = store

export function set(patch: Partial<SkillsToolsState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

export const source = (): SkillsToolsSource => ds('skillsTools')

export function folderBaseName(path: string): string {
  const clean = path.replace(/[\\/]+$/, '')
  if (!clean) return path
  const parts = clean.split(/[\\/]/)
  return parts[parts.length - 1] || clean
}

function readStoredProjectDir(): string | null {
  try {
    const saved = window.localStorage?.getItem(PROJECT_DIR_STORAGE)?.trim()
    return saved || null
  } catch {
    return null
  }
}

function writeStoredProjectDir(dir: string | null): void {
  try {
    if (dir) window.localStorage?.setItem(PROJECT_DIR_STORAGE, dir)
    else window.localStorage?.removeItem(PROJECT_DIR_STORAGE)
  } catch {
    /* private mode or unavailable storage */
  }
}

export function activeSessionWorkdir(): string | null {
  try {
    const sid = sessionCurrent()
    const row = sid ? sess(sid) : undefined
    const wd = row?.workdir?.trim()
    return wd || null
  } catch {
    return null
  }
}

export function recentProjectFolders(): RecentProjectFolder[] {
  const curWd = activeSessionWorkdir()
  const seen = new Set<string>()
  const out: RecentProjectFolder[] = []
  const add = (path: string | null | undefined): void => {
    const clean = (path || '').trim()
    if (!clean || seen.has(clean)) return
    seen.add(clean)
    out.push({ path: clean, name: folderBaseName(clean), current: clean === curWd })
  }
  add(curWd)
  let list: ReturnType<typeof sessionRows> = []
  try {
    list = sessionRows()
  } catch {
    return out
  }
  for (const row of list) {
    if (out.length >= RECENT_PROJECTS_MAX) break
    add(row.workdir)
  }
  return out
}

export function nativeFolderPick(): boolean {
  return servesFolderDialog()
}

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

let refreshTimer: ReturnType<typeof setTimeout> | null = null
export const REFRESH_SOON_MS = 300

export function refreshSoon(): void {
  if (page.get() !== 'skillsToolsPage' || !get().loaded) return
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    void refresh()
    const dir = get().projectDir
    if (dir) void loadProjectSkills(dir)
  }, REFRESH_SOON_MS)
}

let projectEpoch = 0

export async function loadProjectSkills(dir: string): Promise<void> {
  const fetcher = source().projectSkills
  if (!fetcher) {
    set({ projectDir: dir, projectInfo: null, projectLoading: false, projectErr: '' })
    return
  }
  const epoch = ++projectEpoch
  set({ projectDir: dir, projectLoading: true, projectErr: '' })
  try {
    const info = await fetcher(dir)
    if (epoch !== projectEpoch) return
    set({ projectDir: info.projectDir || dir, projectInfo: info, projectLoading: false, projectErr: '' })
  } catch (e) {
    if (epoch !== projectEpoch) return
    const msg = (e as { data?: { detail?: string }; message?: string })?.data?.detail
      || (e as Error)?.message
      || String(e)
    set({ projectInfo: null, projectLoading: false, projectErr: msg })
  }
}

export async function selectProjectDir(dir: string | null): Promise<void> {
  const clean = dir?.trim() || null
  writeStoredProjectDir(clean)
  set({ pickerOpen: false, pickerView: 'menu', pickerErr: '' })
  if (!clean) {
    projectEpoch += 1
    set({ projectDir: null, projectInfo: null, projectLoading: false, projectErr: '' })
    return
  }
  await loadProjectSkills(clean)
}

export async function toggleProjectSkillsEnabled(on: boolean): Promise<void> {
  const ok = await write('skillForge.projectSkills.enabled', on, { remount: false })
  if (ok) {
    const cur = get()
    if (cur.projectInfo) set({ projectInfo: { ...cur.projectInfo, enabled: on } })
  }
}

export function togglePicker(): void {
  const cur = get()
  if (cur.pickerOpen) {
    set({ pickerOpen: false, pickerView: 'menu', pickerErr: '' })
  } else {
    set({ pickerOpen: true, pickerView: 'menu', pickerErr: '' })
  }
}

export function closePicker(): void {
  if (!get().pickerOpen) return
  set({ pickerOpen: false, pickerView: 'menu', pickerErr: '' })
}

export async function browseProjectDir(path?: string): Promise<void> {
  const listDirs = source().listDirs
  if (!listDirs) {
    set({ pickerErr: t('gui.skillsTools.skills.project_browse_unavailable') })
    return
  }
  set({ pickerOpen: true, pickerView: 'browse', pickerLoading: true, pickerErr: '' })
  try {
    const listing = await listDirs(path)
    set({ pickerListing: listing, pickerLoading: false, pickerAsked: get().pickerAsked + 1, pickerErr: '' })
  } catch (e) {
    const msg = (e as { data?: { detail?: string }; message?: string })?.data?.detail
      || (e as Error)?.message
      || String(e)
    set({ pickerLoading: false, pickerAsked: get().pickerAsked + 1, pickerErr: msg })
  }
}

export async function chooseProjectFolder(): Promise<void> {
  if (nativeFolderPick() && source().pickDir) {
    if (get().pickerPicking) return
    set({ pickerPicking: true, pickerErr: '' })
    try {
      const picked = await source().pickDir!()
      set({ pickerPicking: false })
      if (!picked || !picked.path) return
      if (!picked.ok) {
        set({ pickerErr: t('gui.skillsTools.skills.dir_blocked') })
        return
      }
      await selectProjectDir(picked.path)
      return
    } catch {
      set({ pickerPicking: false })
    }
  }
  await browseProjectDir(get().projectDir || undefined)
}

export async function goProjectDir(typed: string): Promise<void> {
  const clean = typed.trim()
  if (!clean) return
  await browseProjectDir(clean)
}

export async function useBrowsedProjectDir(typed?: string): Promise<void> {
  const cur = get().pickerListing
  const clean = typed?.trim()
  if (clean && cur && clean !== cur.path) {
    await browseProjectDir(clean)
    const after = get().pickerListing
    if (after && after.path === clean && after.ok) {
      await selectProjectDir(after.path)
    }
    return
  }
  if (cur && cur.ok) await selectProjectDir(cur.path)
}

export function pickerBack(): void {
  const cur = get()
  if (cur.pickerView === 'drives') {
    set({ pickerView: 'browse', pickerErr: '' })
    return
  }
  set({ pickerView: 'menu', pickerErr: '' })
}

export function pickerDrives(): void {
  set({ pickerView: 'drives', pickerErr: '' })
}

export async function open(tab?: SkillsToolsTab): Promise<void> {
  if (tab) set({ tab, err: '' })
  page.show('skillsToolsPage')
  const curDir = get().projectDir ?? readStoredProjectDir() ?? activeSessionWorkdir()
  const jobs: Promise<void>[] = [refresh(), pkgsLoad()]
  if (curDir) jobs.push(loadProjectSkills(curDir))
  await Promise.all(jobs)
}

export function close(): void {
  closePicker()
  page.show(null)
}

export function setTab(tab: SkillsToolsTab): void {
  if (get().modal) detail.close()
  closePicker()
  set({ tab, err: '' })
  if (tab === 'plugins' && get().pkgs === null) void pkgsLoad()
}

export function detailHost(): HTMLDivElement {
  return detail.host('skillsTools')
}

export async function openSkill(name: string, projectDir?: string): Promise<void> {
  closePicker()
  set({ modal: { kind: 'skill', name, ...(projectDir ? { projectDir } : {}) }, skillDetail: null, err: '' })
  detail.open('skillsTools', { fill: true })
  try {
    const info = await source().inspectSkill(name, projectDir)
    const cur = get().modal
    if (cur?.kind === 'skill' && cur.name === name && cur.projectDir === projectDir) {
      set({ skillDetail: info })
    }
  } catch {
    /* toasted by the source; modal stays on its skeleton */
  }
}

export async function toggleSkillBlocked(name: string, blocked: boolean): Promise<boolean> {
  const ok = await run(`block:${name}`, () => source().blockSkill(name, blocked), { remount: false })
  if (ok) {
    const cur = get()
    if (cur.projectInfo) {
      const folded = name.trim().toLowerCase()
      set({
        projectInfo: {
          ...cur.projectInfo,
          skills: cur.projectInfo.skills.map((s) =>
            s.name.trim().toLowerCase() === folded ? { ...s, blocked } : s,
          ),
        },
      })
    }
  }
  return ok
}

export function openTool(id: string): void {
  closePicker()
  set({ modal: { kind: 'tool', id }, err: '' })
  detail.open('skillsTools')
}

export async function openMcp(name: string): Promise<void> {
  closePicker()
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
  closePicker()
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
  projectEpoch += 1
  askInChat = null
  openProviderFn = null
  writeStoredProjectDir(null)
  store._resetForTests()
  store.set(initial())
}
