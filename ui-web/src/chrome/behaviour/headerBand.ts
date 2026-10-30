/* How much of the transcript's top the chat header covers.
 *
 * The header floats over the scroller rather than sitting above it (page.css,
 * .top), so the scroller's top edge is no longer the first line the reader can
 * see. Anything that brings a turn to the top edge -- the turn nav's marks, the
 * up pill -- has to land it under the band instead, or what it was asked to
 * show is behind the frost.
 *
 * Measured rather than read off the stylesheet: the page sizes the band at
 * --topbar-h, which a desktop shell swaps for the title bar it measured. Zero
 * when the header is not in the document, which is also what keeps the
 * layout-free tests on their old numbers.
 */
export function headerBand(): number {
  return document.querySelector<HTMLElement>('.chat > .top')?.offsetHeight ?? 0
}
