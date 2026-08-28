/* The pane header's two controls have to sit on the same line as each other.
 *
 * They are not the same kind of thing: the fullscreen control holds an inline
 * <svg>, which is laid out on the text baseline with the descender space still
 * under it, and the close control holds the "x" character, which sits wherever
 * the font's metrics put it. Left to those two rules the icon rode 1.5px above
 * the x -- measured in a browser, and visible in the product as two controls
 * that do not line up. Centring the button's content is what makes the pair
 * agree, whatever each one contains.
 *
 * Pinned here because no DOM test can see it: happy-dom does no layout, so the
 * only place this invariant is written down is the stylesheet.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../src/styles/page.css', import.meta.url), 'utf8')

/* One rule's declarations, by exact selector, with comments stripped first --
   what sits between two rules is captured as the next one's selector. */
function rule(selector) {
  const rules = css.replace(/\/\*[\s\S]*?\*\//g, ' ')
  for (const match of rules.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (match[1].trim() === selector) return match[2]
  }
  return null
}

describe('the pane header controls', () => {
  it('centres whatever a header button contains', () => {
    const button = rule('.desk-pane > header button')
    expect(button).toBeTruthy()
    /* Either centring mechanism is fine; what must not happen is neither, which
       is what leaves an inline svg and a text glyph on different lines. */
    const centred = /display:\s*grid[\s\S]*place-items:\s*center/.test(button)
      || /display:\s*flex[\s\S]*align-items:\s*center/.test(button)
    expect(centred).toBe(true)
  })

  it('does not let the line box put the glyph back off centre', () => {
    /* place-items centres the box, and the line box inside it still carries
       half-leading. Measured on the shipped sizes, the two do not contribute
       equally: centring takes the 1.5px gap down to 0.25px, and line-height
       takes that last quarter pixel out. Small, and still the difference
       between "lined up" and "nearly". */
    const button = rule('.desk-pane > header button')
    expect(button).toMatch(/line-height:\s*1\b/)
  })
})
