import { ds, shell, t } from '../../shell/bridge'
import { dropAfterFade } from '../../shell/detailfade'
import { show as toast } from '../../shell/toast'

import type { HubDetail, HubItem, InstalledSkill, SkillsSource } from './types'

/* Page state, outside React on purpose: the legacy shell drives this page
 * imperatively (the skill tab's drawCaps redraws it, a tab switch resets
 * it, the shared drawer's close drops its sheet, a language flip redraws
 * it), so the state lives in a plain store the shims can call, and the
 * component subscribes.
 */

export const HUB_PAGE = 24

export interface SkillsState {
  view: 'market' | 'installed'
  query: string
  cat: string
  page: number
  items: HubItem[]
  total: number
  hub: 'idle' | 'loading' | 'done' | 'error'
  err: string
  /* id of the skill being installed / removed */
  busy: string | null
  drawer: { kind: 'market' | 'inst'; id: string } | null
  /* hub id -> skillhub.detail result, kept for the session */
  details: Record<string, HubDetail>
}

const initial = (): SkillsState => ({
  view: 'market',
  query: '',
  cat: '',
  page: 1,
  items: [],
  total: 0,
  hub: 'idle',
  err: '',
  busy: null,
  drawer: null,
  details: {},
})

let state = initial()
const listeners = new Set<() => void>()

/* The hub search round-trip takes seconds, so page/category flips are
   cached for the session and served instantly; only a genuinely new
   (query, category, page) hits the source -- behind a skeleton grid,
   never a frozen page. A request seq drops stale responses when the
   user outclicks the network. */
const cache = new Map<string, { items: HubItem[]; total: number }>()
let seqNo = 0
let debounce: ReturnType<typeof setTimeout> | undefined

export const getState = (): SkillsState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function set(patch: Partial<SkillsState>): void {
  state = { ...state, ...patch }
  for (const l of listeners) l()
}

export const source = (): SkillsSource => ds<SkillsSource>('skills')
export const view = (): SkillsState['view'] => state.view
export const installedRows = (): InstalledSkill[] => source().installed()

/* True unless the source says its one read never landed. Defaulting to true
   keeps a source that does not answer -- the fixtures -- on the ordinary
   empty copy, which is the truth there. */
export const installedLoaded = (): boolean => {
  const src = source()
  return src.loaded ? src.loaded() : true
}

const errText = (e: unknown): string => {
  const err = e as { data?: { detail?: string }; message?: string } | null
  return (err && err.data && err.data.detail) || (err && err.message) || String(e)
}

/* What a source rejects after toasting the reason itself. */
const ignoreHandled = (e: unknown): void => {
  if (!(e && (e as { handled?: boolean }).handled)) console.error('skills', e)
}

export function search(page = 1): void {
  const key = `${state.query}\u0000${state.cat}\u0000${page}`
  const hit = cache.get(key)
  if (hit) {
    set({ page, items: hit.items, total: hit.total, hub: 'done', err: '' })
    return
  }
  const seq = ++seqNo
  set({ page, hub: 'loading', err: '' })
  source()
    .search({ query: state.query, category: state.cat, page, limit: HUB_PAGE })
    .then((r) => {
      if (seq !== seqNo) return
      const items = r.items || []
      const total = r.total || items.length
      cache.set(key, { items, total })
      if (cache.size > 60) {
        const oldest = cache.keys().next().value
        if (oldest !== undefined) cache.delete(oldest)
      }
      set({ items, total, hub: 'done' })
    })
    .catch((e: unknown) => {
      if (seq !== seqNo) return
      set({ items: [], hub: 'error', err: errText(e) })
    })
}

/* The first reveal of the market fetches; every later draw just renders. */
export function ensureSearch(): void {
  if (state.hub === 'idle' && state.view === 'market') search(1)
}

/* Typing changes no state anyone re-renders on -- the search field is the
   legacy chrome's -- so the query lands without a notify and the fetch is
   debounced behind it. */
export function setQuery(q: string): void {
  state = { ...state, query: q }
  clearTimeout(debounce)
  debounce = setTimeout(() => search(1), 320)
}

export function searchNow(q: string): void {
  clearTimeout(debounce)
  state = { ...state, query: q }
  search(1)
}

export function setCat(cat: string): void {
  if (cat === state.cat) return
  state = { ...state, cat }
  search(1)
}

export function toggleView(): void {
  set({ view: state.view === 'installed' ? 'market' : 'installed', drawer: null })
  shell().closeDetail?.()
}

export function openDetail(kind: 'market' | 'inst', id: string): void {
  set({ drawer: { kind, id } })
}

/* The legacy close path (X, Esc, page switch) already closed #detail;
   this only drops the island's sheet state behind it -- and not until the
   drawer has finished fading, or the card is gone from inside a panel that is
   still on screen. */
export function dropDrawer(): void {
  const was = state.drawer
  if (!was) return
  dropAfterFade(
    () => set({ drawer: null }),
    () => state.drawer !== was,
  )
}

/* The island's own close paths go through the shell so the shared
   #detail dialog and its legacy cousins' state close with the sheet. */
export function closeDrawer(): void {
  shell().closeDetail?.()
  dropDrawer()
}

export function fetchDetail(hubId: string): void {
  source()
    .detail(hubId)
    .then((d) => set({ details: { ...state.details, [hubId]: d } }))
    .catch((e: unknown) => toast(t('gui.hub.err', { err: errText(e) })))
}

export function install(it: HubItem): void {
  set({ busy: it.id })
  source()
    .install(it.id)
    .then(() => {
      it.installed = true
      it.installed_name = it.name
    })
    .catch(ignoreHandled)
    .finally(() => set({ busy: null }))
}

/* Removal of a market row whose installed twin is not in the source (or is
   gone already): the market card just flips back to installable. */
export function removeMarket(it: HubItem): void {
  set({ busy: it.id })
  source()
    .remove(it.installed_name || it.name)
    .then(() => {
      it.installed = false
      it.installed_name = ''
    })
    .catch(ignoreHandled)
    .finally(() => set({ busy: null }))
}

/* Installed rows come from ext.list, so the market row (if any) is found
   by name; removal goes through the same skillhub.remove. */
export function removeInstalled(c: InstalledSkill): void {
  set({ busy: c.id })
  source()
    .remove(c.name)
    .then(() => {
      const it = state.items.find((x) => x.name === c.name || x.installed_name === c.name)
      if (it) {
        it.installed = false
        it.installed_name = ''
      }
      toast(t('gui.caps.removed_x', { name: c.name }))
      closeDrawer()
    })
    .catch(ignoreHandled)
    .finally(() => set({ busy: null }))
}

/* Switching module resets the filters and the view, like every tab flip
   on this page: a query typed while browsing skills is not a question
   about plugins. The session cache stays. */
export function reset(): void {
  clearTimeout(debounce)
  set({ view: 'market', drawer: null, query: '', cat: '' })
}

/* A language flip changes nothing in this state, but every visible string
   comes from t(), so a re-render is the whole redraw. */
export function redraw(): void {
  set({})
}

/* Tests only: back to a cold store, session cache included. */
export function wipe(): void {
  clearTimeout(debounce)
  cache.clear()
  seqNo++
  state = initial()
  for (const l of listeners) l()
}
