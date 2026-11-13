/* The rail's collapse is a slide, and the slide is one movement.
 *
 * The column used to leave the page the way it arrived: `display:none`, and
 * the grid's second track gone with it. Nothing moved, so nothing could be
 * wrong with it -- and then a reader asked for a slide, and a slide is a
 * thing that can be half-done: a column that slides out while the content
 * stays where it was, a column that squashes instead of leaving, a column
 * that goes but stays in the tab order, a content column that grows at one
 * rate while the column beside it leaves at another.
 *
 * So the contract is four claims, and all four are geometric -- happy-dom
 * does no layout, so a rail that is off screen and a rail that is
 * display:none are the same assertion there:
 *
 *   1. The column keeps its place in the grid. A rail that leaves the grid
 *      drops the main pane into the track it left behind, and the whole
 *      right side renders zero-wide.
 *   2. The column leaves as a panel: it keeps the width the drag gave it and
 *      slides out of the window, and the pane grows out of the track under
 *      it, so the content follows the column across.
 *   3. It leaves the tab order on the way, because a rail that is only off
 *      screen is still a rail the keyboard can walk into.
 *   4. One movement, one clock: the column, the pane, the chat header and
 *      the module page move together, because the column's edge rides the
 *      content's edge.
 */

import { describe, expect, it } from 'vitest'

import { css, decls, rules } from './css.mjs'

/* Selector weight as the cascade counts it: ids, then classes / attributes /
   pseudo-classes, then elements. `:has()` weighs what its argument does. */
const weight = (selector) => {
  const ids = (selector.match(/#[\w-]+/g) || []).length
  const classes = (selector.match(/\.[\w-]+|\[[^\]]*\]|:(?!:)(?!(?:has|is|not)\()[\w-]+/g) || []).length
  const elements = (selector.replace(/[#.:[][^\s>+~#.:[]*/g, '').match(/[a-zA-Z][\w-]*/g) || []).length
  return ids * 100 + classes * 10 + elements
}

/* Every duration a transition list names, in the order it names them. */
const durations = (value) => [...(value || '').matchAll(/(\d*\.?\d+)s/g)].map((m) => m[1])

/* The transitions that carry the one movement: the column that leaves, the
   main pane that follows it, the chat header that makes room for the
   collapsed rail's twin, the twin itself -- which arrives with the column
   rather than popping into the corner it has already left -- and the module
   page that takes the whole window. */
const MOVEMENT = ['.rail', '.main', '.chat > .top', '#railShow', '.page']

describe('the rail collapse', () => {
  it('keeps the column in the grid rather than taking it out of it', () => {
    /* A collapsed rule that changed the tracks would change the track COUNT,
       and the pane would land in the one the column left. */
    const tracks = rules(null, css).filter(
      ([selector, body]) => selector.includes('[data-rail="off"]') && /grid-template-columns\s*:/.test(body),
    )
    expect(tracks.map(([selector]) => selector)).toEqual([])
    /* And the column is not display:none, which is what took it out of the
       grid in the first place. */
    expect(decls('.app[data-rail="off"] .rail')?.get('display')).toBe(undefined)
  })

  it('sends the column out as a panel, with the pane taking its place back', () => {
    expect(decls('.rail')?.get('width')).toBe('var(--rail)')
    /* A layout move and not a transform: the content's width changes on the
       same main-thread pass, and a transform would run ahead of it on the
       compositor, leaving the column's edge off the content's. */
    expect(decls('.app[data-rail="off"] .rail')?.get('margin-left')).toBe('calc(-1 * var(--rail))')
    expect(decls('.app[data-rail="off"] .main')?.get('width')).toBe('calc(100% + var(--rail))')
    expect(decls('.app[data-rail="off"] .main')?.get('margin-left')).toBe('calc(-1 * var(--rail))')
  })

  it('takes the column out of the tab order on the way', () => {
    expect(decls('.app[data-rail="off"] .rail')?.get('visibility')).toBe('hidden')
    /* And the transition on it, or the column would be gone before it had
       finished leaving. */
    expect(durations(decls('.rail')?.get('transition'))).toContain('.24')
  })

  it('brings the twin in with the column rather than popping it in', () => {
    /* The twin is the way back, and it stands where the column's own toggle
       stood: if it appears the moment the column starts leaving, the two are
       on screen at once and the reader sees a flash. It fades on the same
       clock instead. */
    expect(decls('#railShow')?.get('transition')).toContain('opacity')
    expect(decls('#railShow')?.get('transition-behavior')).toBe('allow-discrete')
    expect(decls('#railShow[hidden]')?.get('opacity')).toBe('0')
    /* And the style it starts from, since a button that was never rendered has
       no "before" of its own to fade from. */
    expect(decls('#railShow', css)?.get('opacity')).toBe(undefined)
    expect(css).toContain('@starting-style { #railShow { opacity: 0; } }')
  })

  it('moves the whole thing on one clock', () => {
    const clocks = MOVEMENT.map((selector) => durations(decls(selector)?.get('transition')))
    for (const [selector, seen] of MOVEMENT.map((s, i) => [s, clocks[i]])) {
      expect(seen.length, `${selector} carries no transition`).toBeGreaterThan(0)
    }
    /* One clock for all four -- the column's edge rides the content's edge,
       and two clocks would tear them apart. */
    expect(new Set(clocks.flat()), 'every part of the movement runs on one clock').toEqual(new Set(['.24']))
  })

  it('leaves the whole window to the pane under the fold', () => {
    /* Under the fold the rail is an overlay, out of the grid's flow, so the
       pane keeps the window whether the rail stands or not -- and the rule
       that says so has to outweigh the collapsed one, or the pane would be
       a window wide and start 190px to the left of it. */
    const rule = rules(null, css).find(([selector]) => selector === ':root .app[data-rail="off"] .main')
    expect(rule, 'the fold keeps the pane whole').toBeDefined()
    expect(rule[1]).toMatch(/width:\s*100%/)
    expect(rule[1]).toMatch(/margin-left:\s*0/)
    expect(weight(':root .app[data-rail="off"] .main')).toBeGreaterThan(
      weight('.app[data-rail="off"] .main'),
    )
  })

  it('does not ease the module page while the grip is being dragged', () => {
    /* A drag is a measurement, not a state change: the grip writes the
       rail's width as the pointer moves, and an easing inset would trail the
       pointer instead of following it. */
    const rule = rules(null, css).find(
      ([selector]) => selector === ':root:has(#railGrip[data-drag="true"]) .page',
    )
    expect(rule, 'the drag turns the page\'s transition off').toBeDefined()
    expect(rule[1]).toMatch(/transition:\s*none/)
  })
})
