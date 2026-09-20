/* The working directory a conversation runs in: the chip on the composer
 * (#wdChip) and the panel it opens (#wdPop).
 *
 * A writer, not an island, for the same reasons the permission chip is one
 * (see perm.ts): the chip and the panel are static in page.html, the panel is
 * reparented to the body to be positioned, and the state is one string.
 *
 * The choice is made ONCE, while the conversation is still a draft. The engine
 * pins a session's working directory at `session.create` and every later turn
 * reads that pin, so the chip is live only on the new-task screen: the live
 * layer stages the pick (DS.composer.stageWorkdir) and hands it to the create; once
 * a conversation is open the chip only reports where that conversation works
 * and cannot be pressed. Shaped after a code editor's folder switcher -- no
 * folder, the folders recent conversations used, or browse for one -- because
 * that is the picker a person who wants this already knows.
 *
 * "Recent" is derived, not stored: the folders the rail's rows were pinned to,
 * newest activity first. The browser behind "Open folder..." is the gateway's
 * own directory listing (DS.composer.browseDirs, late-bound: the offline demo
 * has no filesystem to show and says so).
 */

import { t } from './bridge'
import { clearance } from './popover'

export interface DirEntry {
  name: string
  path: string
  ok: boolean
}

export interface DirListing {
  path: string
  parent: string | null
  home: string
  ok: boolean
  entries: DirEntry[]
}

type Browse = (path?: string) => Promise<DirListing>
type Stage = (dir: string | null) => void

interface RailRow {
  workdir?: string | null
}

const RECENT_MAX = 6

/* The draft's pick. */
let picked: string | null = null
/* Set while a conversation is open: the directory it was pinned to, or null
   for one on the policy default. `undefined` means the page is on a draft and
   the chip is live. */
let locked: string | null | undefined = undefined
let view: 'menu' | 'browse' = 'menu'
let listing: DirListing | null = null

const el = <T extends HTMLElement>(id: string): T | null => document.getElementById(id) as T | null

/* Both live on the composer seam (DS.composer), installed by the live layer
   beside the promotion the pick feeds; the offline demo installs neither. */
const hooks = (): { stageWorkdir?: Stage; browseDirs?: Browse } => {
  const seam = window.DS as { composer?: { stageWorkdir?: Stage; browseDirs?: Browse } } | undefined
  return seam?.composer ?? {}
}

/* The last path segment, which is what the chip and the rail have room for.
   Windows separators too: the gateway can be on the other platform. */
export function base(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, '')
  const last = trimmed.split(/[\\/]/).pop()
  return last || path
}

/* What the page currently believes the conversation works in. */
export const current = (): string | null => (locked === undefined ? picked : locked)
export const isLocked = (): boolean => locked !== undefined

/* The new-task screen: the chip is live again and starts from no folder. */
export function setDraft(): void {
  locked = undefined
  picked = null
  view = 'menu'
  close()
  draw()
}

/* A conversation is open: the chip reports its directory and stops taking
   presses. */
export function setSession(dir: string | null): void {
  locked = dir
  view = 'menu'
  close()
  draw()
}

/* The folders recent conversations were pinned to, newest first, each once.
   Read from the rail's own rows so the two agree on what "recent" means. */
export function recents(): string[] {
  const seam = window.DS as { sessions?: { snapshot?: () => { rows?: RailRow[] } } } | undefined
  let rows: RailRow[] = []
  try {
    rows = seam?.sessions?.snapshot?.().rows ?? []
  } catch {
    rows = []
  }
  const seen = new Set<string>()
  const out: string[] = []
  for (const r of rows) {
    const d = r.workdir
    if (!d || seen.has(d)) continue
    seen.add(d)
    out.push(d)
    if (out.length >= RECENT_MAX) break
  }
  return out
}

export function draw(): void {
  const chip = el<HTMLButtonElement>('wdChip')
  const name = el('wdName')
  if (!chip || !name) return
  const dir = current()
  name.textContent = dir ? base(dir) : t('gui.wd.none')
  chip.classList.toggle('set', !!dir)
  chip.disabled = isLocked()
  chip.title = isLocked() ? (dir ? `${dir}\n${t('gui.wd.locked')}` : t('gui.wd.locked')) : dir || ''
  chip.setAttribute('aria-label', `${t('gui.wd.title')}: ${dir || t('gui.wd.none')}`)
}

export function open(): void {
  const pop = el('wdPop')
  const chip = el<HTMLButtonElement>('wdChip')
  if (!pop || !chip || isLocked()) return
  view = 'menu'
  render()
  /* Same placement as the permission panel, for the same reason: fixed, measured
     from the chip, clear of the composer card the chip sits on. */
  if (pop.parentElement !== document.body) document.body.appendChild(pop)
  pop.dataset.open = 'true'
  place(pop, chip)
  chip.setAttribute('aria-expanded', 'true')
}

function place(pop: HTMLElement, chip: HTMLElement): void {
  const vw = document.documentElement.clientWidth
  const at = chip.getBoundingClientRect()
  const over = clearance(chip)
  const r = pop.getBoundingClientRect()
  pop.style.position = 'fixed'
  pop.style.left = `${Math.max(8, Math.min(vw - r.width - 8, at.left - 8))}px`
  pop.style.right = 'auto'
  pop.style.top = `${Math.max(8, over.top - r.height - 6)}px`
  pop.style.bottom = 'auto'
  pop.style.zIndex = '46'
}

export function close(): void {
  const pop = el('wdPop')
  if (pop) pop.dataset.open = 'false'
  el('wdChip')?.setAttribute('aria-expanded', 'false')
}

export const isOpen = (): boolean => el('wdPop')?.dataset.open === 'true'

export function toggle(): void {
  if (isOpen()) close()
  else open()
}

/* The pick lands in three places: here for the chip, the live layer's staged
   value for the create, and the panel closes. Null is a pick too -- "no folder"
   takes a staged folder back off. */
function pick(dir: string | null): void {
  picked = dir
  hooks().stageWorkdir?.(dir)
  close()
  draw()
}

function render(): void {
  const body = el('wdBody')
  if (!body) return
  body.textContent = ''
  if (view === 'browse') renderBrowse(body)
  else renderMenu(body)
}

function renderMenu(body: HTMLElement): void {
  body.appendChild(row(t('gui.wd.none'), t('gui.wd.none_h'), picked === null, () => pick(null)))
  const recent = recents()
  if (recent.length) {
    body.appendChild(sec(t('gui.wd.recent')))
    for (const dir of recent) body.appendChild(row(base(dir), dir, picked === dir, () => pick(dir)))
  }
  body.appendChild(rule())
  const openRow = row(t('gui.wd.open'), '', false, () => {
    void browse(picked || undefined)
  })
  openRow.setAttribute('role', 'button')
  openRow.removeAttribute('aria-checked')
  body.appendChild(openRow)
}

/* Ask the gateway for one directory and show it. A missing hook is the demo
   page, which has nothing to browse; a refused path is reported in place and
   the panel stays on whatever it was showing. */
async function browse(path?: string): Promise<void> {
  const ask = hooks().browseDirs
  if (!ask) {
    note(t('gui.wd.not_live'))
    return
  }
  try {
    listing = await ask(path)
  } catch (e) {
    const err = e as { data?: { detail?: string }; message?: string }
    note(t('gui.wd.failed', { detail: (err.data && err.data.detail) || err.message || String(e) }))
    return
  }
  view = 'browse'
  render()
  const pop = el('wdPop')
  const chip = el('wdChip')
  if (pop && chip && isOpen()) place(pop, chip)
}

function renderBrowse(body: HTMLElement): void {
  const l = listing
  if (!l) return
  const bar = document.createElement('div')
  bar.className = 'wdpath'
  bar.appendChild(iconBtn(t('gui.wd.home'), HOME, () => void browse(l.home)))
  const up = iconBtn(t('gui.wd.up'), UP, () => void browse(l.parent || undefined))
  up.disabled = !l.parent
  bar.appendChild(up)
  const p = document.createElement('span')
  p.className = 'p'
  /* Drawn right-to-left so a long path is cut at its head and keeps its tail;
     the marks pin the slashes at both ends where they belong, or the bidi
     algorithm swings the leading one round to the end. */
  p.textContent = `\u200e${l.path}\u200e`
  p.title = l.path
  bar.appendChild(p)
  body.appendChild(bar)

  const list = document.createElement('div')
  list.className = 'wdlist'
  if (!l.entries.length) {
    const empty = document.createElement('div')
    empty.className = 'note'
    empty.textContent = t('gui.wd.empty')
    list.appendChild(empty)
  }
  for (const e of l.entries) {
    const r = row(e.name, '', false, () => void browse(e.path))
    r.setAttribute('role', 'button')
    r.removeAttribute('aria-checked')
    /* `ok` says whether THIS directory may be the working directory, and the
       validator also refuses every ancestor of raven's own data -- so a folder
       that merely contains it answers false while the folders inside it are
       fine. The row stays a way in, marked; only the pick is withheld, which
       the use button below does on the listing's own `ok`. */
    if (!e.ok) {
      r.classList.add('off')
      r.title = t('gui.wd.blocked')
    }
    list.appendChild(r)
  }
  body.appendChild(list)

  const foot = document.createElement('div')
  foot.className = 'wdfoot'
  const back = document.createElement('button')
  back.className = 'mini ghost'
  back.textContent = t('gui.wd.back')
  back.onclick = () => {
    view = 'menu'
    render()
  }
  const use = document.createElement('button')
  use.className = 'mini'
  use.textContent = t('gui.wd.use')
  use.disabled = !l.ok
  if (!l.ok) use.title = t('gui.wd.blocked')
  use.onclick = () => pick(l.path)
  foot.append(back, use)
  body.appendChild(foot)
}

function note(text: string): void {
  const body = el('wdBody')
  if (!body) return
  let n = body.querySelector<HTMLElement>('.note.err')
  if (!n) {
    n = document.createElement('div')
    n.className = 'note err'
    body.appendChild(n)
  }
  n.textContent = text
}

function row(name: string, sub: string, checked: boolean, onPick: () => void): HTMLButtonElement {
  const r = document.createElement('button')
  r.className = 'prow'
  r.setAttribute('role', 'radio')
  r.setAttribute('aria-checked', String(checked))
  const g = document.createElement('span')
  g.className = 'pic'
  g.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${FOLDER}</svg>`
  const txt = document.createElement('span')
  txt.className = 'txt'
  txt.appendChild(span('nm', name))
  if (sub) txt.appendChild(span('sub', sub))
  r.append(g, txt)
  if (checked) r.appendChild(tick())
  r.onclick = onPick
  return r
}

function iconBtn(label: string, path: string, onClick: () => void): HTMLButtonElement {
  const b = document.createElement('button')
  b.className = 'icb'
  b.setAttribute('aria-label', label)
  b.title = label
  b.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">${path}</svg>`
  b.onclick = onClick
  return b
}

const sec = (text: string): HTMLDivElement => {
  const s = document.createElement('div')
  s.className = 'sec'
  s.textContent = text
  return s
}

const rule = (): HTMLDivElement => {
  const s = document.createElement('div')
  s.className = 'rulel'
  return s
}

const span = (cls: string, text: string): HTMLSpanElement => {
  const n = document.createElement('span')
  n.className = cls
  n.textContent = text
  return n
}

const FOLDER = '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.2l1.8 2h9A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/>'
const HOME = '<path d="M4 11.5 12 5l8 6.5"/><path d="M6.5 10v9h11v-9"/>'
const UP = '<path d="M12 19V6"/><path d="m6.5 11.5 5.5-5.5 5.5 5.5"/>'
const CHECK = 'M5 12.5l4.5 4.5L19 7'

function tick(): SVGSVGElement {
  const NS = 'http://www.w3.org/2000/svg'
  const s = document.createElementNS(NS, 'svg')
  s.setAttribute('viewBox', '0 0 24 24')
  s.setAttribute('fill', 'none')
  s.setAttribute('stroke', 'currentColor')
  s.setAttribute('stroke-width', '1.8')
  s.setAttribute('aria-hidden', 'true')
  s.setAttribute('class', 'tick')
  const p = document.createElementNS(NS, 'path')
  p.setAttribute('d', CHECK)
  s.appendChild(p)
  return s
}
