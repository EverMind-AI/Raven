/* The chat header's capsule: which controls it holds, and where they land.
 *
 * Three visibility rules here are the whole of a state no DOM test can see --
 * happy-dom applies no stylesheet, so a control that is hidden and one that is
 * in the row are the same assertion there. The same goes for the two offsets
 * of the anchored palette's notch, and for the container query that folds the
 * tabs to icons once the palette is narrow. Delete any one of them and nothing
 * else in the suite goes red, which is what this file is for.
 *
 * The chip's own counts are a different fact and are covered where they are
 * computed (features/desk tests); this file is about the paint.
 */

import { describe, expect, it } from 'vitest'

import { css, decls, rule, rules } from './css.mjs'

const NOTCH = '.desk-palette[data-anchored="true"]::before'
const READ_ONLY_NOTCH = 'body:has(.chat[data-readonly]) .desk-palette[data-anchored="true"]::before'
const NARROW = '@container (max-width: 250px)'
const FOLDED_TAB = '.desk-tabs button:not([aria-selected="true"])'

/* `display: none` on an exact selector, with the rule's existence checked
   first: a deleted rule and a rule that never said this must not both read as
   "not visible". */
function hiddenBy(selector) {
  const body = decls(selector)
  expect(body, `${selector} is missing from page.css`).toBeTruthy()
  expect(body.get('display'), selector).toBe('none')
}

describe('the chat header capsule', () => {
  it('hides the desk chip on a document with no desk', () => {
    hiddenBy(':root:not(.desk-ready) .desk-follow-toggle')
  })

  it('hides the desk chip while the trajectory view is up', () => {
    hiddenBy('html[data-traj] .desk-follow-toggle')
  })

  it('hides the session menu in an imported session', () => {
    hiddenBy('.chat[data-readonly] #moreBtn')
  })

  /* The other half of each rule above: the chip and the menu are still drawn
     in the ordinary state, and the menu is still one of the capsule's own
     children -- otherwise a rule that hides them would be satisfied by
     controls that were never on screen to begin with. */
  it('still draws them in the ordinary state', () => {
    expect(decls('.desk-follow-toggle')?.get('display')).toBe('inline-flex')
    expect(rule('.chrome-capsule > #moreBtn, .chrome-viewsw + .desk-follow-toggle')).toBeTruthy()
  })
})

describe("the anchored palette's notch", () => {
  it('points back at the chip 48.5px in from the palette\'s right edge', () => {
    expect(decls(NOTCH)?.get('right')).toBe('48.5px')
  })

  /* An imported session has no menu, so the chip ends at the capsule's
     own padding and the notch moves with it. */
  it('follows the chip when the menu leaves the capsule', () => {
    expect(decls(READ_ONLY_NOTCH)?.get('right')).toBe('11.5px')
  })
})

describe('a narrow palette', () => {
  it('folds the unselected tabs to their icons', () => {
    const at = css.indexOf(NARROW)
    expect(at).toBeGreaterThan(-1)
    const first = css.indexOf(`${FOLDED_TAB} {`, at)
    expect(first).toBeGreaterThan(at)
    /* Nothing but the opening brace between the at-rule and its first rule:
       these rules are the query's own children, not rules that merely sit
       after it. */
    expect(css.slice(at, first).trim()).toBe(`${NARROW} {`)
    expect(decls(FOLDED_TAB)?.get('width')).toBe('30px')
    const folded = rules(null).find(([selector]) =>
      selector.includes(`${FOLDED_TAB} .lb`) && selector.includes(`${FOLDED_TAB} .desk-count`))
    expect(folded?.[1]).toMatch(/display:\s*none/)
  })
})
