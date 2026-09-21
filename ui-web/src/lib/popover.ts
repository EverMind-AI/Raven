/* What an anchored panel has to get out of the way of.
 *
 * The three panels that open off the composer -- the permission modes
 * (#permPop), the session tier (#tierPop) and the model picker (.mpick) -- all
 * hang off a chip in the card's bottom bar, and all three measured their
 * clearance from that chip. A chip sits INSIDE the card, so "just above the
 * chip" is over the line the reader is typing on: the panel covered the field,
 * the attachment row and anything staged in it. That line is the one thing
 * still worth seeing while picking a mode or a model -- the whole reason the
 * picker was opened is to change how the next message runs.
 *
 * So the anchor keeps deciding which side of the window a panel hangs from, and
 * this decides how far it has to clear: the composer card when the anchor is on
 * one, the anchor itself when it is not. The fallback is not a defensive
 * default -- the model picker also opens from a settings row, which is not on a
 * card and has nothing to clear but itself.
 *
 * `place` is the two composer popovers' whole geometry, which was the same eight
 * lines in state/perm.ts and state/tier.ts down to the constants. The model
 * pickers keep their own: `.mpick` has two placements (above the composer card
 * from the chip, below the row from a settings slot), and `anchorRow` below
 * serves `components/ModelPicker`, whose last caller is the agents page.
 */

/* The card, not `.dock` around it: the dock is a full-width positioning strip
   with the crew illustrations mounted on it, and clearing that would push every
   panel a further 20-odd pixels up for nothing a reader can see. */
const CARD = '.dock-in'

export function clearance(anchor: Element): DOMRect {
  return (anchor.closest(CARD) ?? anchor).getBoundingClientRect()
}

/* Fixed coordinates measured from the anchor, which is what lets a panel follow
   the composer wherever it sits. The anchor decides the side, `clearance`
   decides the height.
   Read after the panel is shown and out of the card: a hidden panel measures
   zero, and a panel still inside the card is measured against the card. */
export function place(pop: HTMLElement, anchor: HTMLElement): void {
  /* clientWidth, not innerWidth: a backgrounded tab reports the window as 0x0,
     and a panel placed from that lands in a corner. */
  const vw = document.documentElement.clientWidth
  const at = anchor.getBoundingClientRect()
  const over = clearance(anchor)
  const r = pop.getBoundingClientRect()
  pop.style.position = 'fixed'
  pop.style.left = `${Math.max(8, Math.min(vw - r.width - 8, at.left - 8))}px`
  pop.style.right = 'auto'
  pop.style.top = `${Math.max(8, over.top - r.height - 6)}px`
  pop.style.bottom = 'auto'
  /* Above the dock and everything mounted on it: a picker the user just opened
     loses to nothing that was already on screen. The literal ties the two
     popovers to --z-picker's step, which src/state/portals.ts records as a
     deliberate tie broken by the order at the body. */
  pop.style.zIndex = '46'
}

/* The settings dialog, which is the box a panel opened from a settings row has
   to stay inside. `.smodal` is `position: relative` and carries a transform of
   its own, so it is the containing block a `position: fixed` child resolves
   against as well as the box that clips it -- the same arrangement page.css
   records for the add-model drawer, "clipped by this dialog rather than
   floating over the whole window". */
const DIALOG = '.smodal'

/* Where a panel may sit, in viewport coordinates: the dialog it opened inside,
   else the window. */
function bounds(anchor: Element): { top: number; left: number; right: number; bottom: number } {
  const dialog = anchor.closest(DIALOG)
  if (dialog) {
    const r = dialog.getBoundingClientRect()
    return { top: r.top, left: r.left, right: r.right, bottom: r.bottom }
  }
  return { top: 0, left: 0, right: document.documentElement.clientWidth, bottom: document.documentElement.clientHeight }
}

/* Anchor a panel under a row, flipping above it when the room below runs out.
 *
 * `place` above always hangs its panel over the composer; this one is for a row
 * in a list, where below is the reading direction and above is the fallback.
 * The panel is taller than half the dialog, so one side always has room and
 * only one of them needs choosing.
 *
 * Fixed rather than absolute because the row lives inside the settings panel's
 * own scroller (`.spanels`, `overflow-y: auto`), which clips an absolutely
 * positioned child: a 400px popover opened from a row in the lower half lost up
 * to 333 of those pixels. A fixed child resolves against `.smodal` instead,
 * which is above the scroller, so the scroller has nothing to clip.
 *
 * The origin is measured rather than assumed. Which element a fixed box
 * resolves against depends on an ancestor having a transform, and `.smodal`'s
 * comes from an entry animation; reading where `top: 0` actually lands keeps
 * the arithmetic right whether that stays true or not.
 */
export function anchorRow(pop: HTMLElement, anchor: HTMLElement): void {
  const at = anchor.getBoundingClientRect()
  const box = bounds(anchor)
  pop.style.position = 'fixed'
  pop.style.right = 'auto'
  pop.style.bottom = 'auto'
  pop.style.top = '0'
  pop.style.left = '0'
  const origin = pop.getBoundingClientRect()
  const size = { w: origin.width, h: origin.height }
  const below = at.bottom + 6
  const above = at.top - size.h - 6
  const top = below + size.h <= box.bottom - 8 || above < box.top + 8 ? below : above
  const left = Math.min(at.left, box.right - size.w - 8)
  pop.style.top = `${Math.max(box.top + 8, Math.min(top, box.bottom - size.h - 8)) - origin.top}px`
  pop.style.left = `${Math.max(box.left + 8, left) - origin.left}px`
  /* Over the panel it hangs off, and under nothing else in the dialog: the same
     step page.css gives the add-model drawer. */
  pop.style.zIndex = '3'
}
