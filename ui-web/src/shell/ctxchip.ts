/* The composer's context ring.
 *
 * A writer rather than a component: everything it changes lives on one static
 * node the markup already carries (#ctxChip) -- a stroke offset on the ring
 * inside it, two class flips, and the tooltip that doubles as the accessible
 * name. A React root cannot own an svg circle out of someone else's markup,
 * and there is no list here to reconcile.
 *
 * Hidden until a real window is known. A ring drawn from a guessed denominator
 * is worse than no ring: it looks measured.
 */

import { shell } from './bridge'

/* The turn's own usage, as message.complete reports it (context_used /
   context_max). The state is the writer's because the ring is: nothing else
   reads these two numbers. */
let used = 0
let max = 0

/* The ring's circumference, so a percentage can be said as the length of dash
   left to go. Pinned to the markup: page.html draws the circle at r="7.6", and
   2*pi*7.6 is 47.75. The two have to agree or the ring stops at the wrong
   place, and the markup is the half that cannot be changed here. */
const RING = 47.75

const fmtTokens = (n: number): string =>
  n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n)

export function draw(): void {
  const chip = document.getElementById('ctxChip')
  if (!chip) return
  chip.hidden = !max
  if (!max) return
  const pct = Math.min(100, Math.max(0, Math.round((100 * used) / max)))
  chip.classList.toggle('warm', pct >= 70 && pct < 90)
  chip.classList.toggle('hot', pct >= 90)
  const fg = chip.querySelector('.fg')
  if (fg) fg.setAttribute('stroke-dashoffset', String(RING * (1 - pct / 100)))
  /* One string for both, so a mouse and a screen reader are told the same
     thing rather than two versions of it. */
  const tip = shell().T('gui.ctx.tip', {
    used: fmtTokens(used),
    max: fmtTokens(max),
    pct: String(pct),
  })
  chip.dataset.tip = tip
  chip.setAttribute('aria-label', tip)
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
