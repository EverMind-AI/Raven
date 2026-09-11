/* Page state for the playbook library, outside React because the legacy shell
 * opens and closes this page imperatively (the rail button, Escape, a language
 * flip) exactly as it does for knowledge and memory.
 *
 * Two reads, mirroring the two calls: the list is fetched when the page opens,
 * one detail is fetched when a card is opened and then kept until the list is
 * read again -- re-fetching on every node click would put a spinner between a
 * click and its own panel, but a playbook is a file the user can edit, so a
 * cached detail must not outlive the listing it was taken alongside.
 */

import { ds, shell, t } from '../../shell/bridge'
import { show as toast } from '../../shell/toast'

import type { PlaybookDetail, PlaybookRow, PlaybooksCredentialsGetResult, PlaybooksSource } from './types'

export interface PlaybooksState {
  /* null = the list has not been read yet, which is not the same as an empty
     library: one draws a skeleton, the other says the library is empty. */
  rows: PlaybookRow[] | null
  err: string
  query: string
  /* Which playbook is open, and its detail once it lands. */
  openName: string | null
  detail: PlaybookDetail | null
  loading: boolean
  /* The step whose panel is showing, by node id. */
  pickedNode: string | null
  /* Which view of the open playbook is showing. Held here rather than in the
     component so opening another playbook cannot leave the reader on a tab they
     never chose for it. */
  tab: DetailTab
  /* The credentials tab's own reading of the open playbook: null until fetched
     or when the source has no such surface. Kept beside `detail` rather than in
     it because it changes on its own clock -- a save or an authorization
     re-reads it while the playbook itself did not change. */
  creds: PlaybooksCredentialsGetResult | null
  credsLoading: boolean
  /* Per server: the authorization link the flow parked on, while it waits. */
  authUrls: Record<string, string>
  busy: Record<string, boolean>
}

export type DetailTab = 'graph' | 'contract' | 'credentials'

const NO_CREDS = { creds: null, credsLoading: false, authUrls: {}, busy: {} }

const EMPTY: PlaybooksState = {
  rows: null,
  err: '',
  query: '',
  openName: null,
  detail: null,
  loading: false,
  pickedNode: null,
  tab: 'graph',
  ...NO_CREDS
}

let state = EMPTY
const listeners = new Set<() => void>()
/* Details are keyed by name and dropped whenever the list is re-read. */
const details = new Map<string, PlaybookDetail>()

export const getState = (): PlaybooksState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function set(patch: Partial<PlaybooksState>): void {
  state = { ...state, ...patch }
  for (const l of listeners) l()
}

const source = (): PlaybooksSource => ds<PlaybooksSource>('playbooks')

export async function load(): Promise<void> {
  /* Reading the list is the moment the library is looked at afresh, so nothing
     older than this read survives it: neither a cached detail nor the one on
     screen. Closing the page keeps the reader's place, so without the second
     half a playbook opened before an edit stays on screen at its old content
     while its own card shows the new one. */
  details.clear()
  try {
    const rows = await source().list()
    set({ rows, err: '' })
  } catch (e) {
    set({ rows: [], err: (e as Error)?.message || String(e) })
  }
  const stillOpen = state.openName
  if (!stillOpen) return
  /* open() applies the card-open defaults, which are wrong for a refresh: the
     reader did not click this playbook, they were already reading it. Put their
     tab and step back afterwards -- the step only if the edit left it standing,
     since a node can go away between two reads. */
  const tab = state.tab
  const picked = state.pickedNode
  await open(stillOpen)
  if (state.openName !== stillOpen || !state.detail) return
  const stillThere = state.detail.nodes.some(n => n.id === picked)
  set({ tab, pickedNode: stillThere ? picked : state.pickedNode })
}

/* The first node of the graph, so opening a playbook shows a step's panel
   rather than an empty half-page. Deliberately the first *start* node in file
   order and not "whatever id sorts first": the order in the file is the order
   the author wrote, and the panel's job is to be already pointing somewhere
   plausible. */
function firstNode(d: PlaybookDetail): string | null {
  const start = d.nodes.find(n => n.depends_on.length === 0)
  return (start || d.nodes[0])?.id ?? null
}

export async function open(name: string): Promise<void> {
  const cached = details.get(name)
  set({
    openName: name,
    detail: cached ?? null,
    loading: !cached,
    pickedNode: cached ? firstNode(cached) : null,
    tab: 'graph',
    ...NO_CREDS
  })
  if (cached) return
  try {
    const detail = await source().get(name)
    details.set(name, detail)
    /* The reader may have gone back or opened another one while this was in
       flight; a late answer must not repaint the page it no longer belongs to. */
    if (state.openName !== name) return
    set({ detail, loading: false, pickedNode: firstNode(detail) })
  } catch (e) {
    if (state.openName !== name) return
    /* The file can be edited or removed between the listing and this read.
       Leaving the selection set strands the reader on the reading placeholder,
       which carries no way back and survives closing the page, so hand them the
       library and say what happened. */
    back()
    toast((e as Error)?.message || String(e))
  }
}

export function back(): void {
  set({ openName: null, detail: null, pickedNode: null, loading: false, tab: 'graph', ...NO_CREDS })
}

export function showTab(tab: DetailTab): void {
  set({ tab })
  if (tab === 'credentials') void loadCredentials()
}

/* Read what this machine holds for the open playbook. A source without the
   surface leaves `creds` null and the tab says so; a failed read toasts and
   leaves the previous reading in place. */
export async function loadCredentials(): Promise<void> {
  const name = state.openName
  const src = source()
  if (!name || !src.credentials) {
    set({ creds: null, credsLoading: false })
    return
  }
  set({ credsLoading: true })
  try {
    const creds = await src.credentials(name)
    if (state.openName !== name) return
    set({ creds, credsLoading: false })
  } catch (e) {
    if (state.openName !== name) return
    set({ credsLoading: false })
    toast((e as Error)?.message || String(e))
  }
}

function mark(key: string, on: boolean): void {
  set({ busy: { ...state.busy, [key]: on } })
}

export async function saveSecret(param: string, value: string): Promise<void> {
  const name = state.openName
  const src = source()
  if (!name || !src.setSecret || !value) return
  mark('p:' + param, true)
  try {
    await src.setSecret(name, param, value)
    toast(t('gui.pb.cred_saved', { name: param }))
    await loadCredentials()
  } catch (e) {
    toast((e as Error)?.message || String(e))
  } finally {
    mark('p:' + param, false)
  }
}

export async function clearSecret(param: string): Promise<void> {
  const name = state.openName
  const src = source()
  if (!name || !src.clearSecret) return
  mark('p:' + param, true)
  try {
    await src.clearSecret(name, param)
    toast(t('gui.pb.cred_cleared', { name: param }))
    await loadCredentials()
  } catch (e) {
    toast((e as Error)?.message || String(e))
  } finally {
    mark('p:' + param, false)
  }
}

/* Kick the browser flow and remember the link it parked on. The answer comes
   back within seconds with whatever the connect reached; the flow itself runs
   on behind it, so the tab re-reads the credentials on a short cadence until
   the server reads as authorized or the reader leaves the tab. */
export async function authorize(server: string): Promise<void> {
  const name = state.openName
  const src = source()
  if (!name || !src.authorize) return
  mark('s:' + server, true)
  try {
    const res = await src.authorize(name, server)
    if (state.openName !== name) return
    const authUrls = { ...state.authUrls }
    if (res.auth_url) authUrls[server] = res.auth_url
    else delete authUrls[server]
    set({ authUrls })
    if (res.error && res.state !== 'connected') toast(res.error)
    await loadCredentials()
    if (res.state !== 'connected') pollUntilAuthorized(name, server)
  } catch (e) {
    toast((e as Error)?.message || String(e))
  } finally {
    mark('s:' + server, false)
  }
}

const POLL_MS = 3000
const POLL_MAX = 200
function pollUntilAuthorized(name: string, server: string, left: number = POLL_MAX): void {
  if (left <= 0) return
  setTimeout(async () => {
    if (state.openName !== name || state.tab !== 'credentials') return
    await loadCredentials()
    const row = state.creds?.servers.find((s) => s.name === server)
    if (row?.authorized) {
      const authUrls = { ...state.authUrls }
      delete authUrls[server]
      set({ authUrls })
      return
    }
    pollUntilAuthorized(name, server, left - 1)
  }, POLL_MS)
}

export async function clearOauth(server: string): Promise<void> {
  const name = state.openName
  const src = source()
  if (!name || !src.clearOauth) return
  mark('s:' + server, true)
  try {
    await src.clearOauth(name, server)
    toast(t('gui.pb.cred_cleared', { name: server }))
    await loadCredentials()
  } catch (e) {
    toast((e as Error)?.message || String(e))
  } finally {
    mark('s:' + server, false)
  }
}

export function pick(nodeId: string | null): void {
  set({ pickedNode: nodeId })
}

export function search(query: string): void {
  set({ query })
}

/* Rows the list shows: name and description searched together, because a reader
   who types "issue" means either. */
export function visible(): PlaybookRow[] {
  const rows = state.rows || []
  const q = state.query.trim().toLowerCase()
  if (!q) return rows
  return rows.filter(r => `${r.name} ${r.description}`.toLowerCase().includes(q))
}

export function openPage(): void {
  shell().showPage('pbPage')
  void load()
}

export function closePage(): void {
  shell().showPage(null)
}

/* A language flip changes no state here, but every visible string comes from
   t(), so a re-render is the whole redraw. */
export function redraw(): void {
  set({})
}

export function _resetForTests(): void {
  state = EMPTY
  details.clear()
  listeners.clear()
}
