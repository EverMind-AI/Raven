/* The composer's context ring: the two numbers, and the five facts a ring can
 * be drawn from.
 *
 * A store rather than a writer, now that <CtxChip/> renders the chip from it
 * (src/chrome/CtxChip.tsx). The showing, the two warmth classes, the dash left
 * to go and the tooltip that doubles as the accessible name were five writes by
 * id here; they are one state read five ways, and the module keeps the pair of
 * numbers they are derived from because nothing else reads those.
 *
 * Hidden until a real window is known. A ring drawn from a guessed denominator
 * is worse than no ring: it looks measured. Until then the two attributes are
 * absent rather than empty -- the page is served with neither -- which is why
 * the tooltip and the offset are nullable here.
 */

import { flushSync } from 'react-dom'

import { t } from '../i18n/t'

/* The turn's own usage, as message.complete reports it (context_used /
   context_max). The state is the store's because the ring is: nothing else
   reads these two numbers. */
let used = 0
let max = 0

export interface CtxState {
  /** Whether a window is known, which is the only reason to draw a ring. */
  readonly shown: boolean
  readonly warm: boolean
  readonly hot: boolean
  /** The dash left to go, or null while the ring has never been drawn. */
  readonly offset: string | null
  /** One sentence for the hover pill and the accessible name, or null. */
  readonly tip: string | null
}

const served: CtxState = { shown: false, warm: false, hot: false, offset: null, tip: null }
let state: CtxState = served
const listeners = new Set<() => void>()

/** The ring's state, for <CtxChip/>. */
export function get(): CtxState {
  return state
}

/** For useSyncExternalStore: called whenever the ring changes. */
export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/* The ring's circumference, so a percentage can be said as the length of dash
   left to go. Pinned to the markup: the chip draws the circle at r="7.6", and
   2*pi*7.6 is 47.75. The two have to agree or the ring stops at the wrong
   place, and page.css holds the matching stroke-dasharray. */
const RING = 47.75

const fmtTokens = (n: number): string =>
  n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n)

/* Committed synchronously, the way the five writes by id were: a caller that
   asks for a draw and then reads the chip -- the boot list, redrawAll, the
   turn's own completion -- has to see the ring it just asked for. */
function put(next: CtxState): void {
  if (
    next.shown === state.shown && next.warm === state.warm && next.hot === state.hot
    && next.offset === state.offset && next.tip === state.tip
  ) return
  state = next
  flushSync(() => {
    for (const fn of [...listeners]) fn()
  })
}

export function draw(): void {
  /* Only the showing goes when there is no window, which is what the write by
     id did: the classes, the dash and the tooltip keep whatever they had. It
     cannot be reached with any of them set, because no report ever takes a
     window away again -- `set` below only ever raises one. */
  if (!max) {
    put({ ...state, shown: false })
    return
  }
  const pct = Math.min(100, Math.max(0, Math.round((100 * used) / max)))
  /* One string for both, so a mouse and a screen reader are told the same
     thing rather than two versions of it. */
  const tip = t('gui.ctx.tip', {
    used: fmtTokens(used),
    max: fmtTokens(max),
    pct: String(pct),
  })
  put({
    shown: true,
    warm: pct >= 70 && pct < 90,
    hot: pct >= 90,
    offset: String(RING * (1 - pct / 100)),
    tip,
  })
}

/* Two numbers, though the live layer's message.complete carries a third:
   context_estimated, set when the count was derived from a stored transcript
   instead of reported by the provider. It is not taken here because nothing
   renders it -- the tooltip has no marker for an estimate in either language.
   The old version of this code did store it, under a comment promising a
   tilde that no catalogue string has, which is how a field can look load-
   bearing for as long as nobody greps for its readers. Marking an estimate is
   worth doing; it is a change to what the page says, so it belongs in a change
   about that and not in this one. */
export function set(nextUsed: number, nextMax: number): void {
  if (typeof nextMax === 'number' && nextMax > 0) max = nextMax
  if (typeof nextUsed === 'number' && nextUsed >= 0) used = nextUsed
  draw()
}

/* Test seam only: the window the store was told outlives a case now that it is
   the module's rather than the markup's. The subscribers are left alone -- a
   mounted root owns its own, and React takes them back when it unmounts. */
export function _resetForTests(): void {
  used = 0
  max = 0
  state = served
}
