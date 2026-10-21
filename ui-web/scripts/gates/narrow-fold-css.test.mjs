/* The shell's fold under 640px wins the cascade, and the number is one number.
 *
 * Below the fold the window holds one column and a standing rail is an overlay
 * (styles/page.css, the `@media (max-width: 639px)` block). Two of that block's
 * rules override declarations the sheet makes again further down -- the grid's
 * tracks (`.app`) and the module pages' inset (`.page`) -- and both base rules
 * come later at the same weight, so an override spelled with the same selector
 * is simply lost: the first spelling put the main pane back into the second
 * track beside an empty first one whenever the overlay rail stood, which is the
 * squeeze the block exists to end. No DOM test can see a cascade, so the weight
 * is pinned here, computed rather than string-matched.
 *
 * The fold's width lives twice on purpose -- the stylesheet cannot read a
 * TypeScript constant and the boot cannot read a media block -- and this is
 * where the two are held to one value.
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { css, rules } from './css.mjs'

const FOLD = '(max-width: 639px)'

/* Every block under the query, each body by balanced braces: css.mjs's own
   matcher sees the inner rules and not the at-rule around them. More than one
   block may carry the width -- a rule that has to follow its base rule to win
   by order (the hub head's gap) sits beside that rule, not up here. */
function mediaBlocks(query) {
  const bodies = []
  let from = 0
  for (;;) {
    const open = css.indexOf(`@media ${query} {`, from)
    if (open < 0) return bodies
    const brace = css.indexOf('{', open)
    let depth = 0
    for (let i = brace; i < css.length; i++) {
      if (css[i] === '{') depth += 1
      else if (css[i] === '}' && --depth === 0) {
        bodies.push(css.slice(brace + 1, i))
        from = i
        break
      }
    }
    if (depth !== 0) throw new Error(`unbalanced @media ${query} block`)
  }
}

/* The fold itself: the block that sets the grid's tracks. */
function foldBlock() {
  const blocks = mediaBlocks(FOLD).filter((body) => /grid-template-columns/.test(body))
  expect(blocks.length, 'one block folds the grid').toBe(1)
  return blocks[0]
}

/* Selector weight as the cascade counts it: ids, then classes / attributes /
   pseudo-classes, then elements. `:root` is a pseudo-class. */
function weight(selector) {
  const ids = (selector.match(/#[\w-]+/g) || []).length
  const classes = (selector.match(/\.[\w-]+|\[[^\]]*\]|:(?!:)[\w-]+/g) || []).length
  const elements = (selector.replace(/[#.:[][^\s>+~#.:[]*/g, '').match(/[a-zA-Z][\w-]*/g) || []).length
  return ids * 100 + classes * 10 + elements
}

const overrides = (property) =>
  rules(null, foldBlock()).filter(([, body]) => new RegExp(`(^|;)\\s*${property}\\s*:`).test(body))

describe('the fold under 640px', () => {
  it('names the width state/rail.ts folds on, and no other width near it', () => {
    const rail = readFileSync(new URL('../../src/state/rail.ts', import.meta.url), 'utf8')
    expect(rail).toContain(`FOLDS = '${FOLD}'`)
    /* A second number in the six hundreds would be a fold that drifted from
       the one the boot and the rail watch. */
    const near = [...css.matchAll(/@media\s*\((max-width:\s*6\d\dpx)\)/g)].map((m) => `(${m[1]})`)
    expect(near.length).toBeGreaterThan(0)
    expect(new Set(near)).toEqual(new Set([FOLD]))
    foldBlock()
  })

  it.each([
    ['grid-template-columns', '.app'],
    ['left', '.page'],
  ])('outweighs the later base rule for %s on %s', (property, base) => {
    const found = overrides(property)
    expect(found.length, `an override of ${property} in the fold block`).toBeGreaterThan(0)
    for (const [selector] of found) {
      expect(weight(selector), `${selector} against ${base}, which the sheet declares again below the block`)
        .toBeGreaterThan(weight(base))
    }
  })
})
