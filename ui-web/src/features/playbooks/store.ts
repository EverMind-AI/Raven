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

import type {
  StintDetail,
  StintRow,
  PlaybookDetail,
  PlaybookRow,
  PlaybooksCredentialsGetResult,
  PlaybooksSource
} from './types'

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
  /* Which half of the page is showing. The library is what a playbook *is*;
     the runs are what it did, and they outlive the file -- a stint carries its
     own copy of the spec it started with, so a run is still readable after the
     playbook it came from was edited or deleted. */
  view: PageView
  /* null = the runs have not been read yet, which is not an empty list. */
  stints: StintRow[] | null
  plansErr: string
  openStint: StintDetail | null
  plansBusy: boolean
}

export type PageView = 'library' | 'stints'

export type DetailTab = 'graph' | 'contract' | 'credentials'

const NO_CREDS = { creds: null, credsLoading: false, authUrls: {}, busy: {} }

const NO_PLANS = { stints: null, plansErr: '', openStint: null, plansBusy: false }

const EMPTY: PlaybooksState = {
  rows: null,
  err: '',
  query: '',
  openName: null,
  detail: null,
  loading: false,
  pickedNode: null,
  tab: 'graph',
  view: 'library',
  ...NO_CREDS,
  ...NO_PLANS
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

export function showView(view: PageView): void {
  set({ view })
  if (view === 'stints' && state.stints === null) void loadStints()
}

export async function loadStints(): Promise<void> {
  const read = source().stints
  if (!read) {
    /* An engine with no stints surface says so rather than drawing an empty
       list, which reads as "you have never run one". */
    set({ stints: [], plansErr: 'unsupported' })
    return
  }
  try {
    set({ stints: await read.call(source()), plansErr: '' })
  } catch (e) {
    set({ stints: [], plansErr: (e as Error)?.message || String(e) })
  }
}

let opening = 0

export async function openStint(stintId: string): Promise<void> {
  const read = source().stint
  if (!read) return
  /* Two quick clicks are two reads in flight; the later click is the one the
     person means, so an earlier read that lands afterwards is dropped. */
  const ticket = ++opening
  set({ plansBusy: true })
  try {
    const detail = await read.call(source(), stintId)
    if (ticket !== opening) return
    set({ openStint: detail, plansErr: '' })
  } catch (e) {
    if (ticket === opening) set({ plansErr: (e as Error)?.message || String(e) })
  } finally {
    if (ticket === opening) set({ plansBusy: false })
  }
}

/* The list and the open run read again, in place. A run moves while the tab is
   open -- a round finishes, a question lands -- and a page that showed round 1
   of a run on round 3 until the person clicked away read as a stalled run.
   Nothing here touches `plansBusy`, so a refresh never flickers the page. */
export async function refreshStints(): Promise<void> {
  const list = source().stints
  if (!list) return
  try {
    const stints = await list.call(source())
    const open = state.openStint
    const one = source().stint
    const openStint = open && one ? await one.call(source(), open.stint.stint_id) : open
    if (state.openStint !== open) return
    set({ stints, openStint })
  } catch {
    /* The next tick asks again; an error here is not the page's to show. */
  }
}

export function closeStint(): void {
  set({ openStint: null })
}

export async function stopStint(stintId: string): Promise<void> {
  const stop = source().stopStint
  if (!stop) return
  set({ plansBusy: true })
  try {
    const detail = await stop.call(source(), stintId)
    /* The list is patched rather than re-read: a stop changes one row, and
       re-reading would move the reader's place in a list that is sorted by
       when each run started. */
    /* Only while this run is still the one on screen: a person who pressed
       Back before the answer landed does not get the detail pushed back open. */
    const stillOpen = state.openStint?.stint.stint_id === stintId
    set({ ...(stillOpen ? { openStint: detail } : {}), stints: (state.stints || []).map(p => (p.stint_id === stintId ? detail.stint : p)) })
  } catch (e) {
    toast((e as Error)?.message || String(e))
  } finally {
    set({ plansBusy: false })
  }
}

export async function pauseStint(stintId: string): Promise<void> {
  /* `stop` and this differ in what is left behind, not in what happens now:
     both let the round in flight finish and neither opens another, and only a
     paused stint is one `resume` can take up. The list is patched rather than
     re-read for the same reason `stopStint` patches it -- one row changed, and a
     re-read would move the reader's place in a list sorted by start time. */
  const pause = source().pauseStint
  if (!pause) return
  set({ plansBusy: true })
  try {
    const detail = await pause.call(source(), stintId)
    /* Only while this run is still the one on screen: a person who pressed
       Back before the answer landed does not get the detail pushed back open. */
    const stillOpen = state.openStint?.stint.stint_id === stintId
    set({ ...(stillOpen ? { openStint: detail } : {}), stints: (state.stints || []).map(p => (p.stint_id === stintId ? detail.stint : p)) })
  } catch (e) {
    toast((e as Error)?.message || String(e))
  } finally {
    set({ plansBusy: false })
  }
}

export async function resumeStint(stintId: string): Promise<void> {
  /* The verb for a run raven was restarted under, or one somebody paused. The
     round opens in the engine serving this page, so its reports land in the
     conversation that started the run. The driver's sentence is shown as a
     toast: a resume that found nothing to take up says so there. */
  const resume = source().resumeStint
  if (!resume) return
  set({ plansBusy: true })
  try {
    const detail = await resume.call(source(), stintId)
    /* Only while this run is still the one on screen: a person who pressed
       Back before the answer landed does not get the detail pushed back open. */
    const stillOpen = state.openStint?.stint.stint_id === stintId
    set({ ...(stillOpen ? { openStint: detail } : {}), stints: (state.stints || []).map(p => (p.stint_id === stintId ? detail.stint : p)) })
    if (detail.reply) toast(detail.reply)
  } catch (e) {
    toast((e as Error)?.message || String(e))
  } finally {
    set({ plansBusy: false })
  }
}

export async function answerStint(stintId: string, question: number, text: string): Promise<void> {
  const answer = source().answerStint
  if (!answer) return
  set({ plansBusy: true })
  try {
    const detail = await answer.call(source(), stintId, question, text)
    /* Only while this run is still the one on screen: a person who pressed
       Back before the answer landed does not get the detail pushed back open. */
    const stillOpen = state.openStint?.stint.stint_id === stintId
    set({ ...(stillOpen ? { openStint: detail } : {}), stints: (state.stints || []).map(p => (p.stint_id === stintId ? detail.stint : p)) })
  } catch (e) {
    toast((e as Error)?.message || String(e))
  } finally {
    set({ plansBusy: false })
  }
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
