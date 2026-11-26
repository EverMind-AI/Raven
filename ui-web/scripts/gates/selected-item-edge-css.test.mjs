/* The card that marks the selected row, and the edge it wears.
 *
 * Inside the settings dialog the memory list sits on the same paper as the
 * rail beside it, and a selected row is the same white as the rail's
 * selected item -- which is not enough. White on white with no edge reads as
 * "nothing is selected": the row is there, the reader is not told. The rail's
 * item wears a hairline around it, and a list row wore none.
 *
 * The mark is one card that belongs to the list and slides onto whichever row
 * is selected, so a selection is seen to move rather than to blink. The row
 * itself paints nothing: a mark of its own under the sliding one would show
 * as a second mark.
 *
 * The rail's accent bar stays the rail's: it says which section is open, and
 * a row in a list is not a section.
 *
 * happy-dom applies no stylesheet, so no DOM test can see this -- the rule is
 * pinned here, in the sheets that draw it.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { decls, rules } from './css.mjs'

const SETTINGS_SHEET = readFileSync(
  new URL('../../src/features/settings/styles.css', import.meta.url),
  'utf8',
)

/* The parts of the edge a rule wears: the hairline around the item, and the
   accent bar down its left edge. */
const edge = (declarations) =>
  (declarations?.get('box-shadow') ?? '').split(',').map((part) => part.trim())

const hairline = (declarations) => edge(declarations).some((part) => /^inset 0 0 0 1px/.test(part))
const accentBar = (declarations) => edge(declarations).some((part) => /^inset 3px 0 0/.test(part))

/* A custom property's value as the declarations set it, so a gutter written
   once and read through var() is compared as the value it stands for. */
const customProps = (declarations) => {
  const vars = new Map()
  for (const [name, value] of declarations ?? []) {
    if (name.startsWith('--')) vars.set(name, value.trim())
  }
  return vars
}

const resolved = (value, vars) =>
  value === undefined
    ? undefined
    : value
        .replace(/var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)/g, (whole, name, fallback) =>
          vars.has(name) ? vars.get(name) : fallback !== undefined ? fallback.trim() : whole,
        )
        .trim()

/* A value's top-level components, so a calc() stays one component rather than
   splitting at its operators' spaces. */
const splitValues = (value) => {
  const parts = []
  let depth = 0
  let current = ''
  for (const ch of value) {
    if (ch === '(') depth += 1
    else if (ch === ')') depth -= 1
    if (/\s/.test(ch) && depth === 0) {
      if (current) {
        parts.push(current)
        current = ''
      }
    } else {
      current += ch
    }
  }
  if (current) parts.push(current)
  return parts
}

/* The two horizontal sides a rule sets, read the way CSS reads the shorthand:
   one value is every side, two are vertical/horizontal, three are top/
   horizontal/bottom, four are top/right/bottom/left. The `-inline` shorthand
   sets the two inline sides directly, and a left or right longhand sets one.
   Declarations apply in source order, so a shorthand declared after a longhand
   resets it, as the browser paints it. */
const horizontalSides = (declarations, property, vars = customProps(declarations)) => {
  let left
  let right

  const shorthand = (value) => {
    const parts = splitValues(value)
    if (parts.length === 1) [left, right] = [parts[0], parts[0]]
    else if (parts.length === 2 || parts.length === 3) [left, right] = [parts[1], parts[1]]
    else if (parts.length === 4) [left, right] = [parts[3], parts[1]]
  }

  const inlineShorthand = (value) => {
    const parts = splitValues(value)
    if (parts.length === 1) [left, right] = [parts[0], parts[0]]
    else if (parts.length === 2) [left, right] = [parts[0], parts[1]]
  }

  for (const [name, raw] of declarations ?? []) {
    const value = resolved(raw, vars)
    if (name === property) shorthand(value)
    else if (name === `${property}-inline`) inlineShorthand(value)
    else if (name === `${property}-left`) left = value
    else if (name === `${property}-right`) right = value
  }

  return { left, right }
}

const horizontalInsets = (declarations, vars) => horizontalSides(declarations, 'padding', vars)

/* A length's magnitude, ignoring the sign: the list's negative margin and its
   positive padding are the same gutter. A calc() that negates a length reads as
   that length. */
const magnitude = (value) => {
  if (value === undefined) return undefined
  const negated = value.match(/^calc\(\s*-1\s*\*\s*(.+?)\s*\)$/)
  return negated ? negated[1].trim() : value.startsWith('-') ? value.slice(1) : value
}

/* The list's negative margin: it pulls the rows' content edges back out by the
   same gutter the padding pushes them in by, so it has to be that gutter,
   negated. Nothing else in the gate reads it. */
const gutterProblems = (list) => {
  const vars = customProps(list)
  const gutter = vars.get('--two-pane-gutter')
  if (gutter === undefined) return ['the list declares no --two-pane-gutter']

  const problems = []
  const margins = horizontalSides(list, 'margin', vars)
  for (const [side, value] of [['left', margins.left], ['right', margins.right]]) {
    if (value === undefined) {
      problems.push(`the list declares no ${side} margin`)
      continue
    }
    if (value.includes('var(')) {
      problems.push(`the list's ${side} margin does not resolve: ${value}`)
      continue
    }
    if (magnitude(value) !== gutter) {
      problems.push(`the list's ${side} margin is ${value}, the gutter is ${gutter}`)
    }
  }
  return problems
}

/* Everything that would misplace the card against the list as the two rules
   are written: the list has no horizontal padding, or the card steps in by a
   different amount than the list pads on that same side. The two sides need
   not agree -- `padding: 0 6px 2px 5px` with `left: 5px; right: 6px` lines up
   exactly -- so each side is compared with its own, and a card that uses one
   offset where the list uses two fails on the side it does not match. A var()
   resolves through the list's custom properties, with the card's own
   declarations shadowing them, so a card-local override cannot pass silently. */
const edgeProblems = (list, card) => {
  const vars = customProps(list)
  const cardVars = new Map([...vars, ...customProps(card)])
  const { left, right } = horizontalInsets(list, vars)
  if (left === undefined || right === undefined) return ['the list declares no horizontal padding']

  const problems = []
  for (const [side, expected] of [['left', left], ['right', right]]) {
    if (expected.includes('var(')) {
      problems.push(`the list's ${side} does not resolve: ${expected}`)
      continue
    }
    const actual = resolved(card?.get(side), cardVars)
    if (actual === undefined) problems.push(`the card declares no ${side} offset`)
    else if (actual.includes('var(')) problems.push(`the card's ${side} does not resolve: ${actual}`)
    else if (actual !== expected) problems.push(`the card's ${side} is ${actual}, the list's is ${expected}`)
  }
  return problems
}

describe('the card that marks the selected row', () => {
  it('wears a hairline, the way the rail item wears one', () => {
    expect(hairline(decls('.settings-nitem[aria-current="true"]', SETTINGS_SHEET))).toBe(true)
    expect(hairline(decls('.two-pane-sel'))).toBe(true)
  })

  it('leaves the accent bar to the rail, which is where a section is marked', () => {
    expect(accentBar(decls('.settings-nitem[aria-current="true"]', SETTINGS_SHEET))).toBe(true)
    expect(accentBar(decls('.two-pane-sel'))).toBe(false)
  })

  it('slides onto the row that is selected', () => {
    const card = decls('.two-pane-sel')
    expect(card?.get('transition')).toContain('transform')
    expect(card?.get('position')).toBe('absolute')
    /* Under the rows: what the reader reads is the row's own text. */
    expect(card?.get('z-index')).toBe('-1')
    expect(card?.get('pointer-events')).toBe('none')
  })

  it('starts where the rows start, not at the padding box edge', () => {
    /* The list pads its sides so a row's hover ground stops short of the
       scrollbar gutter, and an absolutely positioned child is sized by that
       padding box. The card must step in by the same padding the list applies,
       or it covers the gutter on each side and reads wider than the row it
       marks. */
    const list = decls('.two-pane-list')
    const problems = edgeProblems(list, decls('.two-pane-sel'))
    expect(problems, problems.join('; ')).toEqual([])
    const gutter = gutterProblems(list)
    expect(gutter, gutter.join('; ')).toEqual([])
  })

  it('does not move for a reader who asked for no motion', () => {
    const still = rules(/\.two-pane-sel/).find(([, body]) => body.includes('transition: none'))
    expect(still).toBeTruthy()
  })

  it('leaves the row itself unmarked, so one mark is all there is', () => {
    const row = decls('.two-pane-row[aria-current="true"]')
    expect(row?.get('background')).toBe('none')
    expect(row?.get('box-shadow')).toBeUndefined()
  })
})

describe('reading the list padding', () => {
  it('reads one to four values by their CSS positions', () => {
    const insets = (padding) => horizontalInsets(new Map([['padding', padding]]))
    expect(insets('5px')).toEqual({ left: '5px', right: '5px' })
    expect(insets('1px 5px')).toEqual({ left: '5px', right: '5px' })
    expect(insets('1px 5px 2px')).toEqual({ left: '5px', right: '5px' })
    expect(insets('1px 5px 2px 6px')).toEqual({ left: '6px', right: '5px' })
  })

  it('reads padding-inline, keeps a calc() whole, and follows source order', () => {
    expect(horizontalInsets(new Map([['padding-inline', '4px']]))).toEqual({ left: '4px', right: '4px' })
    expect(horizontalInsets(new Map([['padding-inline', '4px 6px']]))).toEqual({ left: '4px', right: '6px' })
    expect(horizontalInsets(new Map([['padding', '0 5px 2px'], ['padding-left', '7px']]))).toEqual({ left: '7px', right: '5px' })
    /* A shorthand declared after a longhand resets it, as the browser paints it. */
    expect(horizontalInsets(new Map([['padding-left', '7px'], ['padding', '0 5px 2px']]))).toEqual({ left: '5px', right: '5px' })
    /* A calc() is one value, not four: its operators' spaces do not split it. */
    expect(horizontalInsets(new Map([['padding', '0 calc(2px + 3px) 2px 0']]))).toEqual({ left: '0', right: 'calc(2px + 3px)' })
  })

  it('lines up each side on its own, and reads a card-local variable', () => {
    const list = new Map([['--two-pane-gutter', '5px'], ['padding', '0 6px 2px 5px']])
    /* Unequal sides are exact alignment when the card follows each one. */
    expect(edgeProblems(list, new Map([['left', '5px'], ['right', '6px']]))).toEqual([])
    /* A card that uses one offset for both sides cannot match two. */
    expect(edgeProblems(list, new Map([['left', '0'], ['right', '0']])).join('; ')).toContain("card's left is 0")
    /* A card-local custom property shadows the list's, so it cannot pass. */
    const shadowed = new Map([
      ['--two-pane-gutter', '6px'],
      ['left', 'var(--two-pane-gutter)'],
      ['right', 'var(--two-pane-gutter)'],
    ])
    expect(edgeProblems(list, shadowed).join('; ')).toContain("card's left is 6px")
  })

  it('reports a card that steps in by a different amount than the list pads', () => {
    const list = new Map([['padding', '0 6px 2px']])
    const card = new Map([['left', '5px'], ['right', '5px']])
    expect(edgeProblems(list, card).join('; ')).toContain("card's left is 5px")
  })
})
