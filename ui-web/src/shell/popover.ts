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
 * lines in shell/perm.ts and shell/tier.ts down to the constants. The model
 * picker keeps its own, because it hangs BELOW its anchor when there is no room
 * above and clamps to a different margin.
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
