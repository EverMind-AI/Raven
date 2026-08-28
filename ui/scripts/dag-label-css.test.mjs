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

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../src/styles/page.css', import.meta.url), 'utf8')

/* The declarations of one rule, by exact selector, comments stripped first so
   prose that names a selector cannot be read as one. */
function declsOf(selector, source = css) {
  for (const rule of source.replace(/\/\*[\s\S]*?\*\//g, ' ').matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (rule[1].trim() !== selector) continue
    const out = new Map()
    for (const decl of rule[2].split(';')) {
      const at = decl.indexOf(':')
      if (at > 0) out.set(decl.slice(0, at).trim(), decl.slice(at + 1).trim())
    }
    return out
  }
  return null
}

describe('a dag node label', () => {
  /* One line, cut at the end of the box, with the cut marked. Drop any one of
     the three and the label wraps, or spills, or stops with no sign that there
     was more. */
  it('is ended by the box it is in', () => {
    const id = declsOf('.daggraph .nd .id')
    expect(id).not.toBeNull()
    expect(id.get('white-space')).toBe('nowrap')
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
