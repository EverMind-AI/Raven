/* The sub-agent effort tier: the chip on the composer (#tierChip) and the panel
 * it opens (#tierPop).
 *
 * A writer rather than an island, for the reasons `perm.ts` gives next door:
 * both nodes are static in page.html, the panel is reparented to the body to be
 * positioned at all, and the state is one string. The two chips sit side by side
 * and are built the same way on purpose -- a reader meets them as one row of
 * session settings, not as two unrelated controls.
 *
 * What it is NOT is a model. What it IS depends on the catalogue, and the panel
 * says which rather than assuming.
 *
 * A **Session Tier** is `medium`/`high`/`max` and nothing else: `clamp_tier`
 * recognises those three names and declines every other vocabulary, so those are
 * the rungs that actually reach a sub-agent. A **Session Mode** is any profile a
 * deployment declares, and its two knobs -- the iteration cap and the overlay --
 * are the loop's own. CONTEXT.md keeps them as two entries and warns against
 * assuming a replacement catalogue carries a tier.
 *
 * So the heading and the footer are chosen from whether every rung on offer is
 * one the ladder ranks, which is the same test the clamp applies. Naming a
 * deployment's `quick`/`thorough` "sub-agent effort" was not merely loose: those
 * reach no sub-agent at all, and what they do move is raven's own cap.
 *
 * Neither wording claims raven's effort is unchanged, though the shipped
 * catalogue does leave it alone. The reply carries an id, a name and a
 * description -- never the knobs -- so a catalogue that names its rungs
 * `medium`/`high`/`max` AND sets an iteration cap is indistinguishable here from
 * the shipped one. The control says the part it can stand behind: where the tier
 * goes.
 *
 * The menu is the server's, not a constant here. `session.set_mode` answers
 * every call -- report, set and clear alike -- with the tier in force and the
 * whole catalogue, which is why one round trip is enough to draw the control. A
 * deployment can replace the catalogue or turn it off entirely (`"modes": {}`),
 * and an empty menu means this build has no tier to offer: the chip stays
 * hidden rather than drawing a control over nothing.
 *
 * The state lives on the server, so nothing is mirrored to localStorage. A click
 * paints from the REPLY, never from the click: the handler is what decides
 * whether the tier was accepted, and painting first would show a tier that is
 * not in force whenever it refuses.
 */

import { ds, t } from './bridge'
import { show as toast } from './toast'

export interface TierOption {
  id: string
  name?: string
  description?: string
}

export interface TierReply {
  mode?: string | null
  availableModes?: TierOption[]
}

/* The seam, published by the live layer over `session.set_mode` and by the demo
   shell over a fixture. Both calls take the same shape the wire does: no `mode`
   reports, a `mode` switches. */
export interface TierSource {
  read(): Promise<TierReply>
  set(mode: string): Promise<TierReply>
}

let mode = ''
let menu: TierOption[] = []
let loaded = false

/* Who else the tier moves. It is a session-wide setting, and a surface showing
   what one sub-agent will run at is showing this value clamped -- so it has to
   hear about a switch, or it goes on naming the rung from before.

   A notification rather than a call into whoever cares, for the reason the
   composer rack gives for `watchAsking`: this module has no business knowing
   that an instance pane exists. It says the value moved; what that is worth is
   the listener's to decide. */
const WATCHERS = new Set<(next: string) => void>()

export function watch(fn: (next: string) => void): () => void {
  WATCHERS.add(fn)
  return () => WATCHERS.delete(fn)
}

function told(): void {
  WATCHERS.forEach((fn) => {
    try {
      fn(mode)
    } catch {
      /* A listener is a courtesy; one that throws must not take the chip down. */
    }
  })
}

export const current = (): string => mode
export const options = (): readonly TierOption[] => menu

const el = <T extends HTMLElement>(id: string): T | null => document.getElementById(id) as T | null

/* `TIER_LADDER` (`raven/config/schema.py`), cheapest first. Held here because
   the reply cannot say whether a rung is one: it carries ids and names, and
   which of them the clamp ranks is a fact about the ladder, not about the
   catalogue. The one place this side spells the three names. */
const LADDER: readonly string[] = ['medium', 'high', 'max']

/* Two questions, and they are answered at different scopes -- which is the whole
   of what this got wrong the first time.

   What to CALL the catalogue is a fact about the catalogue: every rung on offer
   is one the ladder ranks, or a deployment declared its own vocabulary and this
   is a Session Mode catalogue. Every rather than any, because a mixed one is
   read rung by rung and a heading cannot be half true.

   Where the choice GOES is a fact about the rung in force. `clamp_tier` honours
   an exact hit in any vocabulary that spells the rung the same way, so in a
   `{high, thorough}` catalogue `high` reaches sub-agents and `thorough` does not
   -- one catalogue, two answers. A footer keyed to the catalogue said "not
   offered to sub-agents" over a rung that is. */
const ranked = (): boolean => menu.length > 0 && menu.every((m) => LADDER.includes(m.id))
const inForceReaches = (): boolean => LADDER.includes(mode)

/* Rising bars, one more per rung, so the three read as one control's three
   levels rather than three unrelated pictures -- the same reasoning `perm.ts`
   applies to its shields. A ladder, because that is what a tier is: `medium`,
   `high` and `max` differ in degree, and a metaphor with three unrelated
   symbols would hide the one thing worth seeing at a glance.

   Keyed by the built-in ids. A deployment that replaces the catalogue names its
   own rungs, which this cannot rank, so those draw the neutral glyph rather than
   borrowing a level they were never assigned. */
const ICO: Record<string, string> = {
  medium: '<path d="M6 18.5v-4"/>',
  high: '<path d="M6 18.5v-4"/><path d="M12 18.5v-9"/>',
  max: '<path d="M6 18.5v-4"/><path d="M12 18.5v-9"/><path d="M18 18.5v-14"/>',
}
const FALLBACK = '<circle cx="12" cy="12" r="7.4"/>'

const CHECK = 'M5 12.5 10 17.5 19 7'

function label(id: string): string {
  const found = menu.find((m) => m.id === id)
  /* The catalogue's own name, because a deployment that replaces it names its
     own rungs and this control is not entitled to rename them. Capitalised id
     only when it sent none. */
  return found?.name || (id ? id.charAt(0).toUpperCase() + id.slice(1) : '')
}

/* Hidden until the first reply, and hidden again for a build with no catalogue.
   A chip that draws before the answer arrives has to invent a tier to show, and
   the one it would invent -- the ladder's default -- is exactly the value a
   deployment with its own catalogue does not use. */
export function draw(): void {
  const chip = el('tierChip')
  if (!chip) return
  const off = !loaded || !menu.length
  chip.hidden = off
  if (off) return
  const name = el('tierName')
  if (name) name.textContent = label(mode)
  const pic = chip.querySelector('.pico')
  if (pic) pic.innerHTML = ICO[mode] || FALLBACK
  chip.setAttribute('aria-label', `${t(ranked() ? 'gui.tier.title' : 'gui.tier.mode')}: ${label(mode)}`)
}

/* The panel's heading and footer, written on open because both depend on the
   catalogue that answered. Static markup cannot carry them: page.html is built
   before anything is asked. */
function headings(): void {
  const lab = document.querySelector<HTMLElement>('#tierPop .hd .lab')
  const note = document.querySelector<HTMLElement>('#tierPop .note')
  const tier = ranked()
  if (lab) lab.textContent = t(tier ? 'gui.tier.title' : 'gui.tier.mode')
  /* Keyed to the rung in force, not to `tier`: see `inForceReaches`. */
  if (note) note.textContent = t(inForceReaches() ? 'gui.tier.scope' : 'gui.tier.mode_scope')
}

function absorb(reply: TierReply | null | undefined): void {
  if (!reply) return
  const offered = Array.isArray(reply.availableModes) ? reply.availableModes : []
  const was = mode
  menu = offered.filter((m) => m && typeof m.id === 'string' && m.id)
  mode = typeof reply.mode === 'string' ? reply.mode : ''
  loaded = true
  draw()
  /* Only on a real move. Every read passes through here, including the one on
     each conversation change, and telling listeners the value they already hold
     has changed would have them re-fetch for nothing. */
  if (mode !== was) told()
}

/* Asked on every conversation change, including the change to none: a draft is
   a conversation being written, and the tier its first turn will dispatch at is
   worth showing before that turn is sent. What a draft has no answer for is
   WHERE a switch is written, and that is the seam's problem rather than this
   module's -- see the live source, which stages one the way it already stages a
   model picked before the session exists.

   A failure hides the chip rather than drawing a control whose state is unknown.
   There is nothing useful to show, and a chip that names a tier the next turn
   will not run at is worse than no chip. */
export async function load(): Promise<void> {
  try {
    absorb(await ds<TierSource>('tier').read())
  } catch {
    loaded = false
    draw()
  }
}

export function open(): void {
  const box = el('tierList')
  const pop = el('tierPop')
  const chip = el('tierChip')
  if (!box || !pop || !chip) return
  box.textContent = ''
  headings()
  menu.forEach((m) => {
    box.appendChild(row(m))
  })

  /* Positioned off the chip and reparented to the body for the reason `perm.ts`
     records: the composer card's entrance animation makes it a containing
     block, which quietly re-bases `position: fixed` inside it. */
  if (pop.parentElement !== document.body) document.body.appendChild(pop)
  pop.dataset.open = 'true'
  const vw = document.documentElement.clientWidth
  const at = chip.getBoundingClientRect()
  const r = pop.getBoundingClientRect()
  pop.style.position = 'fixed'
  pop.style.left = `${Math.max(8, Math.min(vw - r.width - 8, at.left - 8))}px`
  pop.style.right = 'auto'
  pop.style.top = `${Math.max(8, at.top - r.height - 6)}px`
  pop.style.bottom = 'auto'
  pop.style.zIndex = '46'
  chip.setAttribute('aria-expanded', 'true')
}

export function close(): void {
  const pop = el('tierPop')
  if (pop) pop.dataset.open = 'false'
  el('tierChip')?.setAttribute('aria-expanded', 'false')
}

export const isOpen = (): boolean => el('tierPop')?.dataset.open === 'true'

export function toggle(): void {
  if (isOpen()) close()
  else open()
}

function row(m: TierOption): HTMLButtonElement {
  const r = document.createElement('button')
  r.className = 'prow'
  r.setAttribute('role', 'radio')
  r.setAttribute('aria-checked', String(m.id === mode))
  const g = document.createElement('span')
  g.className = 'pic'
  g.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICO[m.id] || FALLBACK}</svg>`
  const txt = document.createElement('span')
  txt.className = 'txt'
  txt.append(span('nm', label(m.id)))
  /* A second line under the name, and on the title too for a description too
     long for the row.

     This was hover-only until the base changed under it. The built-in
     descriptions used to read "Sub-agents run at their <tier> tier. Raven's own
     effort is the same in every mode." -- the first half restating the name and
     the second identical on every row, which the footer already makes once, and
     neither translated. They now say only what differs between the rungs
     (`_TIER_TEXTS`), and raven translates the ones it owns, so the reason for
     hiding them is gone and what is left is worth reading. A deployment's own
     descriptions are its author's and come through untouched, in whatever
     language they wrote. */
  if (m.description) {
    txt.append(span('sub', m.description))
    r.title = m.description
  }
  r.append(g, txt)
  if (m.id === mode) r.appendChild(tick())
  r.onclick = () => {
    close()
    if (m.id === mode) return
    void choose(m.id)
  }
  return r
}

/* The reply decides, not the click. The handler refuses a tier this build does
   not offer, and painting the row first would leave the chip showing a tier the
   next turn will not run at. */
async function choose(id: string): Promise<void> {
  try {
    absorb(await ds<TierSource>('tier').set(id))
  } catch (err) {
    toast(t('gui.tier.failed', { name: label(id) }))
    /* Nothing moved -- `mode` still holds what the server last told us -- but
       the panel may be redrawn from it, so put the chip back in step. */
    draw()
    void err
  }
}

const span = (cls: string, text: string): HTMLSpanElement => {
  const n = document.createElement('span')
  n.className = cls
  n.textContent = text
  return n
}

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

/* Test seam: the module's state outlives a test file's DOM. */
/* Test seam: the module's state outlives a test file's DOM. Watchers are NOT
   cleared, for the reason `composer/sheets` gives -- one is registered when its
   own module loads, which happens once per test file, so clearing them would
   unwire the first reset and leave every case after it listening to nothing. */
/* Test seam for a listener's side of the contract: the tier moving without a
   reply to absorb, which is what a caller in another module has to react to. */
export function _notifyForTests(next: string): void {
  mode = next
  told()
}

export function _resetForTests(): void {
  mode = ''
  menu = []
  loaded = false
}
