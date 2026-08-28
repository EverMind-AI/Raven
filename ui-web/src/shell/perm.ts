/* The permission mode: the chip on the composer (#permChip) and the panel it
 * opens (#permPop).
 *
 * A writer, not an island, and the reason is in the markup: both the chip and
 * the panel are static in page.html, the panel has to be REPARENTED to the body
 * to be positioned at all (see below), and the whole state is one string. A
 * component would own two nodes in two different places and re-render a radio
 * group of three.
 *
 * Three tiers, ordered from the strictest to the one with no brakes, because
 * that is the order a reader should meet them in. Front end only for now:
 * nothing in the engine reads the choice yet, and the panel says so rather than
 * letting the row imply a guarantee it cannot keep. The default is the
 * behaviour Raven actually has today -- full access -- and it is the one tier
 * drawn in the warning colour, since that is a fact about it, not decoration.
 *
 * So this module is also the whole of where the choice lives: a value in
 * memory, mirrored to localStorage so it survives a reload.
 */

import { t } from './bridge'

interface Tier {
  id: string
  label: string
  sub: string
  risk?: boolean
}

export const TIERS: readonly Tier[] = [
  { id: 'ask', label: 'gui.perm.ask', sub: 'gui.perm.ask_h' },
  { id: 'smart', label: 'gui.perm.smart', sub: 'gui.perm.smart_h' },
  { id: 'full', label: 'gui.perm.full', sub: 'gui.perm.full_h', risk: true },
]

const KEY = 'raven.perm'

/* Shields, one per tier, differing only in what is inside them: a question, a
   check, an exclamation. Same outline so the three read as one control's three
   states rather than three unrelated icons. */
const ICO: Record<string, string> = {
  ask:
    '<path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/>'
    + '<path d="M9.6 10.2a2.4 2.4 0 1 1 3.3 2.2v1.1"/><path d="M12 16h.01"/>',
  smart:
    '<path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/>'
    + '<path d="M9 12.2l2 2 4-4.4"/>',
  full:
    '<path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/>'
    + '<path d="M12 8.6v3.6M12 15.2h.01"/>',
}

const CHECK = 'M5 12.5l4.5 4.5L19 7'

/* Read once, and validated: a stored id from an older build that no longer
   names a tier would leave the chip drawing nothing. Private mode throws on
   read, which is a reason to fall back rather than a reason to fail. */
let mode = read()

function read(): string {
  let stored = ''
  try {
    stored = localStorage.getItem(KEY) || ''
  } catch {
    stored = ''
  }
  return TIERS.some((p) => p.id === stored) ? stored : 'full'
}

export const current = (): string => mode

const el = <T extends HTMLElement>(id: string): T | null => document.getElementById(id) as T | null

/* The chip reads its own state, which is why it carries no hover label: the
   detail of each tier belongs in the panel the click opens. Nothing to remove
   for that -- page.html ships #permChip with no data-tip and no data-i18n-tip
   for applyI18n to fill, and the legacy drawPerm's `delete chip.dataset.tip`
   was dead there too. It did not come across. */
export function draw(): void {
  const cur = TIERS.find((p) => p.id === mode) || TIERS[0]!
  const name = el('permName')
  if (name) name.textContent = t(cur.label)
  const chip = el('permChip')
  if (!chip) return
  chip.classList.toggle('risk', !!cur.risk)
  const pic = chip.querySelector('.pico')
  if (pic) pic.innerHTML = ICO[cur.id] || ICO.full!
  chip.setAttribute('aria-label', `${t('gui.perm.title')}: ${t(cur.label)}`)
}

export function open(): void {
  const box = el('permList')
  const pop = el('permPop')
  const chip = el('permChip')
  if (!box || !pop || !chip) return
  box.textContent = ''
  TIERS.forEach((p) => {
    box.appendChild(row(p))
  })

  /* Off the CHIP itself, not off the composer card: the card's static anchor
     left the panel hanging the field's whole height above the button it came
     from. Fixed coordinates measured from the chip put its bottom edge right on
     the control, wherever the composer happens to sit -- and the pop has to
     leave the card's DOM for that, because the card's entrance animation makes
     it a containing block that quietly re-bases position: fixed. */
  if (pop.parentElement !== document.body) document.body.appendChild(pop)
  pop.dataset.open = 'true'
  /* clientWidth/Height, not innerWidth/Height: a backgrounded tab reports the
     window as 0x0, and a panel placed from that lands in a corner. */
  const vw = document.documentElement.clientWidth
  const at = chip.getBoundingClientRect()
  const r = pop.getBoundingClientRect()
  pop.style.position = 'fixed'
  pop.style.left = `${Math.max(8, Math.min(vw - r.width - 8, at.left - 8))}px`
  pop.style.right = 'auto'
  pop.style.top = `${Math.max(8, at.top - r.height - 6)}px`
  pop.style.bottom = 'auto'
  /* Above the dock and everything mounted on it: a mode picker the user just
     opened loses to nothing that was already on screen. */
  pop.style.zIndex = '46'
  chip.setAttribute('aria-expanded', 'true')
}

export function close(): void {
  const pop = el('permPop')
  if (pop) pop.dataset.open = 'false'
  el('permChip')?.setAttribute('aria-expanded', 'false')
}

export const isOpen = (): boolean => el('permPop')?.dataset.open === 'true'

/* The chip toggles rather than opens: it is the only way back out of the panel
   with the pointer, since the panel has no close button of its own. */
export function toggle(): void {
  if (isOpen()) close()
  else open()
}

function row(p: Tier): HTMLButtonElement {
  const r = document.createElement('button')
  r.className = 'prow' + (p.risk ? ' risk' : '')
  r.setAttribute('role', 'radio')
  r.setAttribute('aria-checked', String(p.id === mode))
  const g = document.createElement('span')
  g.className = 'pic'
  g.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICO[p.id]}</svg>`
  const txt = document.createElement('span')
  txt.className = 'txt'
  txt.append(span('nm', t(p.label)), span('sub', t(p.sub)))
  r.append(g, txt)
  if (p.id === mode) r.appendChild(tick())
  r.onclick = () => {
    mode = p.id
    /* Private mode throws on write. Losing the preference is the whole cost,
       and it is not worth taking the click down with it. */
    try {
      localStorage.setItem(KEY, mode)
    } catch {
      /* nothing to do about it */
    }
    draw()
    close()
  }
  return r
}

const span = (cls: string, text: string): HTMLSpanElement => {
  const n = document.createElement('span')
  n.className = cls
  n.textContent = text
  return n
}

/* The same svg the legacy ico() built, attribute for attribute: this one wears
   the stroke on the element rather than taking it from the stylesheet, unlike
   the shields above, and the two are not interchangeable. */
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
