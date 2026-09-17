/* The upgrade progress card held over the page while serve is unavailable.
 *
 * State rather than a writer: the card is one node at the body (drawn by
 * src/chrome/UpgradeShade.tsx) and the handle open() returns is what the two
 * callers address it by -- the update watcher, which says how far along the
 * install is, and the connection watcher, which raises the same card when the
 * server disappears under a version it already knows about.
 *
 * One card at a time, and the newest wins: opening takes down whatever was
 * there, and a handle to a card that has been replaced goes quiet rather than
 * writing into a node that is no longer on screen.
 */

import { flushSync } from 'react-dom'

import { t } from './bridge'

export interface UpgradeShade {
  say(text: string): void
  fail(text: string, detail?: string): void
  close(): void
}

/* The three lines the manual path adds, with their labels taken when the
   failure happened rather than at every render: the card is already on screen
   by then, and a language flip must not rewrite it underneath the reader. */
export interface Failure {
  readonly sub: string
  readonly copy: string
  readonly close: string
}

export interface Card {
  readonly id: number
  readonly text: string
  readonly failure?: Failure
}

export const COMMAND = 'raven upgrade'

let card: Card | null = null
let seq = 0
const listeners = new Set<() => void>()

export const get = (): Card | null => card

export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

function commit(next: Card | null): void {
  card = next
  flushSync(() => { for (const fn of [...listeners]) fn() })
}

/** The card's own close button, which closes that card and no later one. */
export function dismiss(id: number): void {
  if (card && card.id === id) commit(null)
}

export function copyCommand(): void {
  if (navigator.clipboard) navigator.clipboard.writeText(COMMAND)
}

export function open(): UpgradeShade {
  commit(null)
  /* A card this module did not draw -- the page can be showing one from before
     a reload -- is taken down the same way the writer took every `.upshade`
     down before building its own. */
  document.querySelectorAll('.upshade').forEach((node) => node.remove())
  const id = ++seq
  commit({ id, text: '' })
  const mine = (): boolean => !!card && card.id === id
  return {
    say(text: string): void {
      if (mine()) commit({ ...card!, text })
    },
    fail(text: string, detail?: string): void {
      if (!mine()) return
      commit({
        ...card!,
        text,
        failure: {
          sub: detail ? `${detail}\n${t('gui.upg.manual')}` : t('gui.upg.manual'),
          copy: t('gui.dtl.copy'),
          close: t('gui.upg.close'),
        },
      })
    },
    close(): void {
      dismiss(id)
    },
  }
}

/** Test seam only: the card outlives a test. */
export function _resetForTests(): void {
  commit(null)
}
