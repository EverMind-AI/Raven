/* The context window: the ring on the composer's bar, and the popover it opens.
 *
 * One store for both, the way state/perm.ts is one store for the permission
 * chip and the popover off it: the numbers the ring draws are the numbers the
 * popover reads, and two stores would have two writers for one window.
 *
 * Hidden until a real window is known. A ring drawn from a guessed denominator
 * is worse than no ring: it looks measured. Until then the two attributes are
 * absent rather than empty -- the page is served with neither -- which is why
 * the tooltip and the offset are nullable here.
 *
 * The parts are the window divided: what is filling it, not only how full it
 * is. They are an estimate of one request -- the loop measures the prompt it
 * sends -- and they are absent until a turn has measured one. A window nobody
 * divided is still a window, so the popover says so rather than guessing.
 */

import { t } from '../i18n/t'
import { makeStore } from './store'

import type { CtxParts } from '../lib/ctxGrid'

/* The turn's own usage, as message.complete reports it (context_used /
   context_max). The state is the store's because the ring is: nothing else
   reads these two numbers. */
let used = 0
let max = 0
let parts: CtxParts | null = null

/** The window divided, in tokens: what is in it, by part. */

export interface CtxState {
  /** Whether a window is known, which is the only reason to draw a ring. */
  readonly shown: boolean
  readonly warm: boolean
  readonly hot: boolean
  /** The dash left to go, or null while the ring has never been drawn. */
  readonly offset: string | null
  /** One sentence for the hover pill and the accessible name, or null. */
  readonly tip: string | null
  /** The window's fill and its size, in tokens. */
  readonly used: number
  readonly max: number
  /** The fill as a percentage, clamped to the ring's world: full is full.
      Derived here so the ring and the popover cannot each round it their own
      way -- the card said 200% beside a ring that had stopped at 100. */
  readonly pct: number
  /** The window divided, or null while nothing has divided it. */
  readonly parts: CtxParts | null
  /** The popover's up-or-down. */
  readonly open: boolean
}

const served: CtxState = {
  shown: false,
  warm: false,
  hot: false,
  offset: null,
  tip: null,
  used: 0,
  max: 0,
  pct: 0,
  parts: null,
  open: false,
}
const store = makeStore<CtxState>(served)

/** The window's state, for <CtxChip/> and <CtxPopover/>. */
export const { get, subscribe } = store

/** For useSyncExternalStore: called whenever the window changes. */
/* The ring's circumference, so a percentage can be said as the length of dash
   left to go. Pinned to the markup: the chip draws the circle at r="7.6", and
   2*pi*7.6 is 47.75. The two have to agree or the ring stops at the wrong
   place, and page.css holds the matching stroke-dasharray. */
const RING = 47.75

const fmtTokens = (n: number): string =>
  n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n)

/* Committed synchronously: a caller that asks for a draw and then reads the
   chip -- the boot list (app/boot.ts), the language repaint
   (state/lang/effects.ts), the turn's own completion -- has to see the ring it
   just asked for. */
/* The same fields are not a write: this runs on every report the gateway
   sends, and most of them move nothing the reader can see. */
function unchanged(next: CtxState): boolean {
  const now = get()
  return next.shown === now.shown && next.warm === now.warm && next.hot === now.hot
    && next.offset === now.offset && next.tip === now.tip
    && next.used === now.used && next.max === now.max && next.pct === now.pct
    && next.parts === now.parts && next.open === now.open
}

export function draw(): void {
  /* Only the showing goes when there is no window, which is what the write by
     id did: the classes, the dash and the tooltip keep whatever they had. A
     window that is left is taken by `clear`, which writes the whole reading
     back to nothing rather than reaching this branch with a stale sentence;
     nothing else takes one away, since `set` below only ever raises one. */
  if (!max) {
    const off = { ...get(), shown: false, open: false }
    if (!unchanged(off)) store.set(off)
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
  const next: CtxState = {
    shown: true,
    warm: pct >= 70 && pct < 90,
    hot: pct >= 90,
    offset: String(RING * (1 - pct / 100)),
    tip,
    used,
    max,
    pct,
    parts,
    /* The popover belongs to a window: with none, it has nothing to say and
       nothing to stand open over. */
    open: max ? get().open : false,
  }
  if (!unchanged(next)) store.set(next)
}

/* Three numbers, though the live layer's message.complete carries a fourth:
   context_estimated, set when the count was derived from a stored transcript
   instead of reported by the provider. It is not taken here because nothing
   renders it -- the tooltip has no marker for an estimate in either language.
   The old version of this code did store it, under a comment promising a
   tilde that no catalogue string has, which is how a field can look load-
   bearing for as long as nobody greps for its readers. Marking an estimate is
   worth doing; it is a change to what the page says, so it belongs in a change
   about that and not in this one. */
export function set(
  nextUsed: number | undefined,
  nextMax: number | undefined,
  nextParts?: Partial<CtxParts> | null,
): void {
  if (typeof nextMax === 'number' && nextMax > 0) max = nextMax
  if (typeof nextUsed === 'number' && nextUsed >= 0) used = nextUsed
  if (nextParts) {
    parts = {
      system: nextParts.system ?? 0,
      tools: nextParts.tools ?? 0,
      conversation: nextParts.conversation ?? 0,
      files: nextParts.files ?? 0,
      memory: nextParts.memory ?? 0,
      skills: nextParts.skills ?? 0,
    }
  }
  draw()
}

/* The parts are one conversation's: `set` keeps them across a report that
   brings none, which is right within a conversation and wrong across two. What
   is left behind is the whole reading, not only the parts -- the fill and the
   window described that conversation as much as the breakdown did, and a new
   task has measured nothing at all. The ring and the popover go with them: an
   open card would otherwise show the conversation just left. */
export function clear(): void {
  used = 0
  max = 0
  parts = null
  /* `served` is the whole reading at nothing, so it takes the stale percentage
     and sentence with it; `draw` cannot, because its no-window branch keeps
     what it had. */
  if (!unchanged(served)) store.set(served)
}

/** The popover's up-or-down. The chip is the only way back out of it. */
export function toggle(): void {
  if (!max) return
  store.set({ ...get(), open: !get().open })
}

/** The popover shut. A click elsewhere or a keypress closes it. */
export function close(): void {
  if (!get().open) return
  store.set({ ...get(), open: false })
}

/* Test seam only: the window the store was told outlives a case now that it is
   the module's rather than the markup's. The subscribers are left alone -- a
   mounted root owns its own, and React takes them back when it unmounts. */
export function _resetForTests(): void {
  used = 0
  max = 0
  parts = null
  store._resetForTests()
}
