/* The selectors and the one measurement the page's writers reach the document
 * by.
 *
 * `document.querySelector` under a shorter name, which is how the page's
 * writers have always spelled it. It stays one name rather than forty call
 * sites for a second reason:
 * a case that drives one of those modules builds the part of the page it is
 * about and no more, and standing in for this is how it gets away with that
 * (scripts/module-harness.mjs's looseQuery).
 *
 * headerBand() reads layout off the page instead; it lives here because it is
 * a document read like the selector, not a feature's own concern.
 */
export const $ = (selector: string): HTMLElement | null =>
  document.querySelector(selector)

/* How far below the band a jump leaves its target, so the line it brought the
 * reader to is not flush with the frost. Shared by the two jumps that land on
 * a turn (features/composer/userjump.ts, features/transcript/TurnNav.tsx) so
 * they agree on where the band ends. */
export const HEADER_LEAD = 16

/* How much of the transcript's top the chat header covers.
 *
 * The header floats over the scroller rather than sitting above it (page.css,
 * .chat > .top), and its frost is the header row itself. That lower edge is the
 * first line the reader can see sharp, so anything that brings a turn to the
 * top edge (the turn nav's marks, the up pill) has to land it under the row
 * instead, or what it was asked to show is behind the frost.
 *
 * Measured for the row: the row's height is what a desktop shell swaps for the
 * title bar it measured, so a resize and shell mode both reach the band without
 * a second number here. `scroll` scopes the header lookup to the conversation
 * that owns it, so two chat columns cannot measure each other's. Without a
 * header in the document the band is zero, which is also what keeps the
 * layout-free tests on their old numbers.
 */
export function headerBand(scroll?: HTMLElement | null): number {
  const top = scroll?.closest('.chat')?.querySelector<HTMLElement>('.top') ?? $('.chat > .top')
  if (!top) return 0
  return top.offsetHeight
}
