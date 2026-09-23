/* The one selector helper the page's writers reach the document by.
 *
 * `document.querySelector` under a shorter name, which is how the page's
 * writers have always spelled it. It stays one name rather than forty call
 * sites for a second reason:
 * a case that drives one of those modules builds the part of the page it is
 * about and no more, and standing in for this is how it gets away with that
 * (scripts/module-harness.mjs's looseQuery).
 */
export const $ = (selector: string): HTMLElement | null =>
  document.querySelector(selector)
