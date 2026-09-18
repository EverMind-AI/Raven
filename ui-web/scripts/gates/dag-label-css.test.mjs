/* A dag node's label is cut by the node, and the stylesheet is where that is
 * written.
 *
 * The label used to be SVG text whose length was decided by measuring it in the
 * document and cutting until it fitted -- once, with the answer remembered. A
 * graph measured where nothing has a width, or before its font arrived, kept an
 * answer taken in the dark: the labels then ran past their boxes and across the
 * node beside them, for as long as the page was open. Nothing measures now. The
 * text is laid out inside the box and the three declarations below are what end
 * the line, at whatever width the box is and whenever the font turns up.
 */

import { describe, expect, it } from 'vitest'

import { decls as declsOf } from './css.mjs'

describe('a dag node label', () => {
  /* Two lines, cut at the end of the box, with the cut marked. A step is
     titled by a sentence rather than by an id, and one line of a 196px box is
     not a sentence -- so the clamp replaced the `nowrap`, and it is the clamp
     that now ends the text. What this case is about is unchanged: the box ends
     the label, and the reader can see that it did. Drop the clamp or the
     overflow and the title spills across the step beside it. */
  it('is ended by the box it is in', () => {
    const id = declsOf('.daggraph .nd .id')
    expect(id).not.toBeNull()
    expect(id.get('-webkit-line-clamp')).toBe('2')
    expect(id.get('display')).toBe('-webkit-box')
    expect(id.get('overflow')).toBe('hidden')
    expect(id.get('text-overflow')).toBe('ellipsis')
    /* And it must be allowed to be narrower than its text, or the flex line
       grows to the content and there is nothing to overflow. */
    expect(id.get('min-width')).toBe('0')
  })

  /* The agent name shares the second line with the clock, and it is the half
     that gives way. */
  it('gives the same treatment to the agent name under it', () => {
    const ag = declsOf('.daggraph .nd .ag')
    expect(ag).not.toBeNull()
    expect(ag.get('text-overflow')).toBe('ellipsis')
    expect(ag.get('min-width')).toBe('0')
    expect(declsOf('.daggraph .nd .tm').get('flex')).toBe('none')
  })

  /* The label is HTML inside the SVG, so without this it would take the hover
     and the click that belong to the node under it -- the tooltip carrying the
     untruncated text would stop appearing over the very text it completes. */
  it('does not take the clicks that belong to the node', () => {
    expect(declsOf('.daggraph .nd .lbl').get('pointer-events')).toBe('none')
  })
})
