/* The session search row (#findBox in the markup).
 *
 * A writer, not an island, and for a plainer reason than the other writers
 * here: the row is three static elements the stylesheet already lays out, and
 * a component owning them would re-render a text field the reader is typing
 * into. What this module owns is the term.
 *
 * The term used to be a page global the chrome wrote and the rail's snapshot
 * carried back in. It is neither now: it lives here, and the rail asks for it
 * through term() below -- a direct read inside one bundle, not a seam. A seam
 * would mean the demo and live layers each having to answer for a value only
 * this module can produce, which is exactly the coupling the DataSource seam
 * exists to retire.
 *
 * Redraws go out through the shell rather than calling the rail island's draw:
 * the live boot layer REBINDS drawList (live/010-boot-guard.js) to keep the
 * skeleton up until the first real list lands, and a stored reference here
 * would keep calling whichever function existed when this module evaluated.
 */

import { composing } from '../features/composer/store'

import { shell } from './bridge'

let query = ''

/* What the rail filters by. Lowercased and trimmed at write time, so every
   reader compares against the same shape. */
export function term(): string {
  return query
}

const el = <T extends HTMLElement>(id: string): T | null => document.getElementById(id) as T | null

/* Clearing is three facts, not one: the field, the term, and the clear button
   that only exists while there is something to clear. Kept together because
   every caller that clears one clears all three. */
function clear(): void {
  const field = el<HTMLInputElement>('sfind')
  if (field) field.value = ''
  query = ''
  const clr = el('sclr')
  if (clr) clr.hidden = true
}

/* Search is a chore, so it hides until asked for; leaving it empty and
   clicking away puts the row back. */
export function toggle(force?: boolean): void {
  const box = el('findBox')
  if (!box) return
  const open = force != null ? force : box.hidden
  box.hidden = !open
  el('findBtn')?.setAttribute('aria-expanded', String(open))
  if (open) {
    el<HTMLInputElement>('sfind')?.focus()
    return
  }
  /* Closing on an empty field changes nothing, so it must not cost a redraw:
     the blur handler below closes the row on every click away. */
  if (query) {
    clear()
    shell().drawList?.()
  }
}

export function install(): void {
  const field = el<HTMLInputElement>('sfind')
  const clr = el('sclr')
  const btn = el('findBtn')

  if (field) {
    field.oninput = () => {
      query = field.value.trim().toLowerCase()
      if (clr) clr.hidden = !query
      shell().drawList?.()
    }
    field.onkeydown = (e) => {
      /* Escape ends an open composition first; taking the row down on that
         keystroke would discard the candidate the reader was still typing. */
      if (composing(e)) return
      if (e.key === 'Escape') {
        e.stopPropagation()
        toggle(false)
      }
    }
    field.onblur = () => {
      if (!query) toggle(false)
    }
  }

  if (clr) {
    clr.onclick = () => {
      clear()
      shell().drawList?.()
      el<HTMLInputElement>('sfind')?.focus()
    }
  }

  if (btn) btn.onclick = () => toggle()
}
