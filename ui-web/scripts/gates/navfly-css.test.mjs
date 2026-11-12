/* The More group's popover: hidden until its row opens it, and where it opens.
 *
 * happy-dom applies no stylesheet, so a popover that is `display: none` and
 * one painted on every page load are the same assertion there -- delete the
 * hidden rule and nothing else in the suite goes red. These are the paint
 * rules the door depends on, read from src/styles/page.css.
 */

import { describe, expect, it } from 'vitest'

import { css, decls } from './css.mjs'

const NAVFLY = '.chrome-navfly'
const OPEN = '.chrome-navfly[data-open="true"]'

describe('the More group popover', () => {
  it('is hidden until its row opens it', () => {
    const body = decls(NAVFLY)
    /* The rule's existence first: a deleted rule and one that never said
       `display: none` must not both read as "not visible". */
    expect(body, `${NAVFLY} is missing from page.css`).toBeTruthy()
    expect(body.get('display')).toBe('none')
  })

  it('is drawn once the row opens it', () => {
    expect(decls(OPEN)?.get('display')).toBe('block')
  })

  it('hangs 15px past the rail, anchored to its row', () => {
    expect(decls('.chrome-navfly-anch')?.get('position')).toBe('relative')
    const body = decls(NAVFLY)
    expect(body.get('position')).toBe('absolute')
    expect(body.get('left')).toBe('calc(100% + 15px)')
  })

  it('opens in place under the fold', () => {
    /* Only the at-rule's opening brace and whitespace between the query and
       its first rule, so this is the 639px block that carries the popover
       rather than one of the page's other three. */
    const at = css.search(/@media \(max-width: 639px\) \{\s*\.chrome-navfly \{/)
    expect(at).toBeGreaterThan(-1)
    expect(decls(NAVFLY, css.slice(at))?.get('position')).toBe('static')
  })
})
