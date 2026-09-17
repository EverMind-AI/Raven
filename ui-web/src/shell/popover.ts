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
 */

/* The card, not `.dock` around it: the dock is a full-width positioning strip
   with the crew illustrations mounted on it, and clearing that would push every
   panel a further 20-odd pixels up for nothing a reader can see. */
const CARD = '.dock-in'

export function clearance(anchor: Element): DOMRect {
  return (anchor.closest(CARD) ?? anchor).getBoundingClientRect()
}
