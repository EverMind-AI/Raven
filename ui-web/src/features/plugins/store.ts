import { ds, shell, t } from '../../shell/bridge'
import { dropAfterFade } from '../../shell/detailfade'
import { show as toast } from '../../shell/toast'

import type {
  DetailEntry,
  InstalledRow,
  MarketItem,
  McpSnapshot,
  PluginsEvent,
  PluginsSource,
} from './types'

/* Page state, outside React on purpose: the legacy shell drives this page
 * imperatively (the tab chrome opens it, the search bar feeds it, gateway
 * events advance its installs), so the state lives in a plain store the
 * shims can call, and the component subscribes.
 */

export interface Drawer {
  kind: 'market' | 'inst' | 'progress'
  id: string
}

/* Install progress: the sheet drives the whole transaction -- write config
   -> connect -> (browser authorization) -> done. Closing the sheet leaves
   the install running and this state alive; it is released when the story
   ends (done/fail acknowledged or resolved off-screen, cancel, uninstall). */
export interface Prog {
  id: string
  name: string
  mode: string
  host: string
  step: number
  state: 'run' | 'done' | 'fail'
  err: string
  tools: number | null
}

export interface PlugState {
  view: 'market' | 'installed'
  items: MarketItem[]
  cats: string[]
  marketState: 'idle' | 'loading' | 'done' | 'error'
  err: string
  query: string
  cat: string
  busy: string | null
  drawer: Drawer | null
  form: boolean
  confirm: boolean
  prog: Prog | null
}

let state: PlugState = {
  view: 'market',
  items: [],
  cats: [],
  marketState: 'idle',
  err: '',
  query: '',
  cat: '',
  busy: null,
  drawer: null,
  form: false,
  confirm: false,
  prog: null,
}
const listeners = new Set<() => void>()

export const getState = (): PlugState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function set(patch: Partial<PlugState>): void {
  state = { ...state, ...patch }
  for (const l of listeners) l()
}

export const source = (): PluginsSource => ds<PluginsSource>('plugins')

/* Installs whose authentication hasn't been proven yet: while an id is in
   here the entry stays out of every "installed" surface, and the pending
   install resolves from events -- connected -> installed, a settled auth
   failure -> the install is rolled back. */
const pending = new Set<string>()
const marketIds = new Set<string>()
const authWait: Record<string, string> = Object.create(null)
const authEnd: Record<string, number> = Object.create(null)
let authClock: ReturnType<typeof setInterval> | null = null
let debounce: ReturnType<typeof setTimeout> | null = null

const msg = (e: unknown): string => ((e as Error | null)?.message ?? String(e)) || String(e)
const isHandled = (e: unknown): boolean => Boolean(e && (e as { handled?: boolean }).handled)

export const rows = (): InstalledRow[] => source().rows()

/* See the skills store: an empty list means two different things and only
   the source knows which. */
export const rowsLoaded = (): boolean => {
  const src = source()
  return src.loaded ? src.loaded() : true
}
export const mcpRows = (): InstalledRow[] => rows().filter((p) => p.m)
export const pyRows = (): InstalledRow[] => rows().filter((p) => !p.m)

export const authUrl = (id: string): string | undefined => authWait[id]
export const authEndsAt = (id: string): number | undefined => authEnd[id]
export const pendingHas = (id: string): boolean => pending.has(id)
export const fromMarket = (id: string): boolean => marketIds.has(id)
export const view = (): string => state.view
export const installedCount = (): number =>
  rows().filter((p) => !p.m || !pending.has(p.m.name)).length

export const entryMcp = (entry: DetailEntry) =>
  (entry.contributes || []).find((c) => c.kind === 'mcp') || null

export const hostOf = (url?: string): string => {
  try {
    return new URL(url || '').host
  } catch {
    return url || ''
  }
}

/* Status vocabulary: a dot plus a word, never color alone. */
export const PM_ST: Record<string, { k: string; cls: string }> = {
  off: { k: 'gui.plug.st_off', cls: 'off' },
  connecting: { k: 'gui.plug.st_conn', cls: 'busy' },
  connected: { k: 'gui.plug.st_on', cls: 'ok' },
  auth_required: { k: 'gui.plug.st_auth', cls: 'bad' },
  error: { k: 'gui.plug.st_err', cls: 'bad' },
  disconnected: { k: 'gui.plug.st_off', cls: 'off' },
}

export function status(m: McpSnapshot): { k: string; cls: string } {
  if (authWait[m.name]) return { k: 'gui.plug.st_wait', cls: 'busy' }
  if (!m.enabled) return PM_ST.off!
  return PM_ST[m.state] || PM_ST.off!
}

export const authLeft = (id: string): string => {
  const end = authEnd[id]
  if (!end) return ''
  const s = Math.max(0, Math.round((end - Date.now()) / 1000))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

/* Writes the remaining time into whatever is showing it, rather than
   re-rendering the panel once a second: same discipline as the legacy page. */
function authTick(on: boolean): void {
  if (on && !authClock) {
    authClock = setInterval(() => {
      if (!Object.keys(authEnd).length) {
        authTick(false)
        return
      }
      document.querySelectorAll('[data-authcd]').forEach((el) => {
        el.textContent = authLeft((el as HTMLElement).dataset.authcd || '')
      })
    }, 1000)
  } else if (!on && authClock && !Object.keys(authEnd).length) {
    clearInterval(authClock)
    authClock = null
  }
}

/* The chrome resync: re-render locally, then ask the shell to redraw the
   tab chrome it owns (title, hero, the installed button) when the plugin
   tab is actually showing -- the legacy pmRedraw guard. */
function sync(): void {
  set({})
  shell().plugRedraw?.()
}

export function redraw(): void {
  set({})
}

/* A tab switch away resets the filters and the view, like every module. */
export function reset(): void {
  set({ view: 'market', drawer: null, query: '', cat: '' })
}

export function setQuery(q: string): void {
  set({ query: q })
  searchSoon()
}

export function setCat(cat: string): void {
  set({ cat })
  void search()
}

export function searchSoon(): void {
  if (debounce) clearTimeout(debounce)
  debounce = setTimeout(() => void search(), 320)
}

export function searchIfIdle(): void {
  if (state.marketState === 'idle') void search()
}

export async function search(): Promise<void> {
  set({ marketState: 'loading', err: '' })
  shell().plugRedraw?.()
  try {
    const r = await source().search(state.query, state.cat)
    r.items.forEach((it) => marketIds.add(it.id))
    set({ items: r.items, cats: r.categories, marketState: 'done' })
  } catch (e) {
    set({ items: [], err: msg(e), marketState: 'error' })
  }
  shell().plugRedraw?.()
}

export function toggleView(): void {
  set({ view: state.view === 'installed' ? 'market' : 'installed' })
  drawerClosed()
  shell().plugRedraw?.()
}

export function backToMarket(): void {
  set({ view: 'market' })
  drawerClosed()
  shell().plugRedraw?.()
}

/* ── drawer ──────────────────────────────────────────────────────── */

export function openDetail(kind: Drawer['kind'], id: string, keep?: boolean): void {
  // While this plugin's install is still running, every entry point lands
  // on the progress sheet -- closing it must never strand the install.
  if (kind !== 'progress' && state.prog && state.prog.id === id && state.prog.state === 'run') {
    kind = 'progress'
  }
  // An installed market plugin opens the full catalog detail; only
  // manually-configured servers fall through to the slim config drawer.
  if (kind === 'inst' && marketIds.has(id)) kind = 'market'
  const patch: Partial<PlugState> = { drawer: { kind, id } }
  if (!keep) {
    patch.form = false
    patch.confirm = false
  }
  set(patch)
}

/* Every close path lands here -- the legacy closeDetail wrapper, Esc, the
   X, and the island's own buttons. A running install keeps its prog so the
   sheet can be reentered from the card; a settled one is acknowledged. */
export function drawerClosed(): void {
  const was = state.drawer
  if (!was) return
  /* The flag now, the card in a moment. Every caller here is either the legacy
     close (which flips the flag itself, a line later) or one of the island's
     own buttons (which had no other way to close the drawer than by unmounting
     the card). Both want the fade to start at once -- and both wanted the card
     to still be in it, which is what unmounting in the same tick took away.
   *
   * Written straight onto the element rather than through `shell().closeDetail`:
   * the legacy closer is wrapped to call back into here, so going out through
   * the shell would be a loop. */
  const detail = document.getElementById('detail')
  if (detail) detail.dataset.open = 'false'
  dropAfterFade(
    () => {
      const patch: Partial<PlugState> = { drawer: null, form: false, confirm: false }
      if (state.prog && state.prog.state !== 'run') patch.prog = null
      set(patch)
    },
    () => state.drawer !== was,
  )
}

export function unfoldForm(id: string): void {
  set({ form: true })
  openDetail('market', id, true)
}

export function unfoldConfirm(id: string): void {
  set({ confirm: true })
  openDetail('market', id, true)
}

export function foldForms(id: string): void {
  set({ form: false, confirm: false })
  openDetail('market', id, true)
}

/* ── install progress ────────────────────────────────────────────── */

function progOpen(entry: DetailEntry): void {
  const mcp = entryMcp(entry)
  set({
    prog: {
      id: entry.id,
      name: entry.name,
      mode: mcp ? (mcp.auth || {}).mode || 'none' : 'none',
      host: mcp ? hostOf((mcp.connection || {}).url) : '',
      step: 0,
      state: 'run',
      err: '',
      tools: 0,
    },
  })
  openDetail('progress', entry.id)
}

/* The sheet is authoritative while it shows this id: toasts for the same
   outcome would just repeat it. */
const progShows = (id: string): boolean =>
  Boolean(state.prog && state.prog.id === id && state.drawer && state.drawer.kind === 'progress')

function progStep(n: number): void {
  const pg = state.prog
  if (pg && pg.state === 'run' && n > pg.step) set({ prog: { ...pg, step: n } })
}

/* tools === null means "installed, still connecting": the sheet closes the
   story as installed and a later connected event fills the tool count in. */
function progDone(tools: number | null): void {
  const pg = state.prog
  if (!pg) return
  if (pg.state !== 'run' && !(pg.state === 'done' && pg.tools == null)) return
  const next: Prog = { ...pg, state: 'done', tools }
  // Resolved while the sheet is closed: the toast carries the news and the
  // plugin goes back to opening its normal detail.
  set({ prog: progShows(pg.id) ? next : null })
}

function progFail(err: string): void {
  const pg = state.prog
  if (!pg || pg.state !== 'run') return
  const next: Prog = { ...pg, state: 'fail', err: err || '' }
  set({ prog: progShows(pg.id) ? next : null })
}

/* ── actions ─────────────────────────────────────────────────────── */

export function install(entry: DetailEntry, form: Record<string, string>): void {
  // Pending from the first moment: the ledger lands on disk mid-call, and the
  // entry must not read as installed anywhere before its auth is proven.
  set({ busy: entry.id })
  pending.add(entry.id)
  progOpen(entry)
  sync()
  source()
    .install(entry.id, form || {})
    .then((r) => {
      const it = state.items.find((x) => x.id === entry.id)
      if (r.pending) {
        // Guard on the pending mark: a cancel mid-call already rolled the
        // install back, and marking it installed here would resurrect it.
        if (pending.has(entry.id)) {
          if (it) it.installed = false
          if (!authWait[entry.id] && !progShows(entry.id)) {
            toast(t('gui.plug.wait_auth', { name: entry.name }))
          }
        }
      } else {
        pending.delete(entry.id)
        if (it) it.installed = true
        const st = r.mcp && r.mcp.state
        if (st === 'connected') {
          progDone((r.mcp && r.mcp.tool_count) || 0)
          if (!progShows(entry.id)) {
            toast(
              t('gui.plug.installed_ok', { name: entry.name, n: (r.mcp && r.mcp.tool_count) || 0 }),
            )
          }
        } else {
          if (state.prog && state.prog.id === entry.id) progDone(null)
          if (!progShows(entry.id)) toast(t('gui.plug.installed_conn', { name: entry.name }))
        }
      }
      set({ form: false, confirm: false })
      return source().reload()
    })
    .catch((e: unknown) => {
      // Includes the backend's own rollback (auth settled as failed inside
      // the connect window): nothing is installed, clear the pending mark.
      pending.delete(entry.id)
      const err = msg(e)
      if (state.prog && state.prog.id === entry.id) progFail(err)
      if (!progShows(entry.id)) toast(t('gui.plug.op_failed', { err }))
      return source()
        .reload()
        .catch(() => {})
    })
    .finally(() => {
      set({ busy: null })
      sync()
    })
}

/* Card-level install: fetch the manifest, install straight away when nothing
   needs input; anything with a key form or a run-locally confirm opens the
   sheet already unfolded at that step. */
export function quickInstall(it: MarketItem): void {
  set({ busy: it.id })
  sync()
  source()
    .detail(it.id)
    .then(({ entry }) => {
      const mcp = entryMcp(entry)
      const mode = mcp ? (mcp.auth || {}).mode || 'none' : 'none'
      const stdio = Boolean(mcp && (mcp.connection || {}).command)
      if (mode === 'apikey' || stdio) {
        set({ busy: null, form: mode === 'apikey', confirm: mode !== 'apikey' })
        openDetail('market', entry.id, true)
        return
      }
      install(entry, {})
    })
    .catch((e: unknown) => {
      set({ busy: null })
      if (!isHandled(e)) toast(t('gui.plug.op_failed', { err: msg(e) }))
      sync()
    })
}

/* A pending install failed authentication: the plugin never counted as
   installed, so undo the disk transaction and put the market card back. */
export function pendingFail(name: string, why: string): void {
  if (!pending.has(name)) return
  pending.delete(name)
  delete authWait[name]
  const it = state.items.find((x) => x.id === name)
  if (it) it.installed = false
  /* The cause travels with the failure: "the authorization window closed"
     tells the reader what to do differently on the retry. */
  if (state.prog && state.prog.id === name) progFail(why || '')
  if (!progShows(name)) toast(t('gui.plug.auth_fail_rm', { name: it ? it.name : name }))
  source()
    .remove(name)
    .catch(() => {})
    .then(() => sync())
}

export function remove(name: string, label: string): void {
  set({ busy: name })
  sync()
  source()
    .remove(name)
    .then(() => {
      const it = state.items.find((x) => x.id === name)
      if (it) it.installed = false
      pending.delete(name)
      delete authWait[name]
      // Cancel/uninstall ends the install story outright.
      if (state.prog && state.prog.id === name) set({ prog: null })
      toast(t('gui.caps.removed_x', { name: label || name }))
      drawerClosed()
    })
    .catch((e: unknown) => {
      if (!isHandled(e)) toast(t('gui.plug.op_failed', { err: msg(e) }))
    })
    .finally(() => {
      set({ busy: null })
      sync()
    })
}

export function cancelPending(id: string, label: string): void {
  pending.delete(id)
  remove(id, label)
}

export function auth(name: string): void {
  set({ busy: name })
  sync()
  source()
    .auth(name)
    .then((r) => {
      const row = mcpRows().find((p) => p.m && p.m.name === name)
      if (r && row && row.m) Object.assign(row.m, r)
    })
    .catch(() => {})
    .finally(() => {
      set({ busy: null })
      sync()
    })
}

export function toggleMcp(name: string, on: boolean): void {
  const row = mcpRows().find((p) => p.m && p.m.name === name)
  if (row && row.m) row.m.enabled = on
  sync()
  source()
    .toggle(name, on)
    .then((r) => {
      if (r && row && row.m) Object.assign(row.m, r)
      sync()
    })
    .catch(() => sync())
}

export function togglePy(row: InstalledRow): void {
  const on = row.state !== 'on'
  source()
    .togglePy(row, on)
    .catch(() => {})
    .then(() => sync())
}

/* ── events from the gateway (forwarded by the live source) ──────── */

/* A connect that parked before any client attached broadcast its oauth.pending
   to nobody, so the URL only reaches us on the pull. Seeded into authWait rather
   than read off the row at render time, so it has ONE lifetime: everything that
   already clears a live authorization -- oauth.done, a settled mcp.status --
   clears this too. Read from the row instead and a plugin that finished
   authorizing kept showing "waiting" behind a dead link until a full reload. */
function seedPulledAuth(): void {
  for (const row of mcpRows()) {
    const m = row.m
    if (!m || !m.auth_url) continue
    /* Only while the row itself still says the park is live. A URL on a row
       whose state has settled is a stale read, and seeding from it would put
       back a link the user has already finished with. */
    if (m.state !== 'auth_required' && m.state !== 'connecting') continue
    if (!authWait[m.name]) authWait[m.name] = m.auth_url
  }
}

export function onEvent(ev: PluginsEvent): void {
  if (ev.kind === 'rows') {
    seedPulledAuth()
    sync()
    return
  }
  if (ev.kind === 'status') {
    // A settled status for the busy server means nothing is processing any
    // more -- belt-and-braces against a busy flag that outlives its install.
    const inFlight = state.busy === ev.name
    if (inFlight && ev.state !== 'connecting') set({ busy: null })
    if (state.prog && state.prog.id === ev.name) {
      if (ev.state === 'connecting') progStep(1)
      else if (ev.state === 'connected') progDone(ev.tool_count || 0)
    }
    if (pending.has(ev.name)) {
      if (ev.state === 'connected') {
        pending.delete(ev.name)
        const it = state.items.find((x) => x.id === ev.name)
        if (it) it.installed = true
        // The install RPC reports its own outcome; only a later async
        // connect (the OAuth round-trip) announces from here.
        if (!inFlight && !progShows(ev.name)) {
          toast(
            t('gui.plug.installed_ok', { name: it ? it.name : ev.name, n: ev.tool_count || 0 }),
          )
        }
      } else if ((ev.state === 'auth_required' || ev.state === 'error') && !inFlight) {
        // While the install RPC is in flight the backend rolls back itself
        // and the call rejects; acting here too would remove twice.
        pendingFail(ev.name, ev.error || '')
        return
      }
    }
    /* The event is authoritative for this too: it carries auth_url on every
       snapshot, null included, so a park that settled clears the seeded pull
       without waiting for another rows load. */
    if (ev.state !== 'connecting' || !ev.auth_url) delete authWait[ev.name]
    if (ev.auth_url) authWait[ev.name] = ev.auth_url
    sync()
    return
  }
  if (ev.kind === 'authPending') {
    authWait[ev.server] = ev.url
    /* The window has an end, so the page shows one: without it nothing
       distinguishes a flow still worth finishing from one that expired. */
    if (ev.expires_in) authEnd[ev.server] = Date.now() + Number(ev.expires_in) * 1000
    authTick(true)
    if (state.prog && state.prog.id === ev.server) progStep(2)
    /* A background connect found this server unauthorized; say so where the
       reader can act on it -- the rows grow a "reopen" button off authWait. */
    if (!progShows(ev.server)) {
      toast(
        t(ev.interactive === false ? 'gui.plug.auth_needed' : 'gui.plug.auth_opened', {
          host: hostOf(ev.url),
          name: ev.server,
        }),
      )
    }
    sync()
    return
  }
  delete authWait[ev.server]
  delete authEnd[ev.server]
  authTick(false)
  if (ev.ok && state.prog && state.prog.id === ev.server) progStep(3)
  if (!ev.ok) {
    const why = ev.error === 'timeout' ? t('gui.plug.auth_expired') : ev.error || ''
    // A failed authorization on a pending install rolls the install back;
    // on an already-installed server (re-auth) it just reports. While the
    // install RPC is in flight the backend rolls back itself.
    if (pending.has(ev.server)) {
      if (state.busy !== ev.server) pendingFail(ev.server, why)
      return
    }
    toast(why || t('gui.plug.auth_fail', { name: ev.server }))
  }
  sync()
}
