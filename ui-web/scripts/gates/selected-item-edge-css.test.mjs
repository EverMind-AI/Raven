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

/* The two sides a rule pads by, read the way CSS reads the shorthand: one
   value is every side, two are vertical/horizontal, three are top/horizontal/
   bottom, four are top/right/bottom/left. `padding-inline` sets the two inline
   sides directly and a lone left/right longhand wins over either. */
const horizontalInsets = (declarations, vars = customProps(declarations)) => {
  const get = (prop) => resolved(declarations?.get(prop), vars)
  let left
  let right

  const shorthand = get('padding')
  if (shorthand !== undefined) {
    const parts = shorthand.split(/\s+/)
    if (parts.length === 1) [left, right] = [parts[0], parts[0]]
    else if (parts.length === 2 || parts.length === 3) [left, right] = [parts[1], parts[1]]
    else if (parts.length === 4) [left, right] = [parts[3], parts[1]]
  }

  const inline = get('padding-inline')
  if (inline !== undefined) {
    const parts = inline.split(/\s+/)
    if (parts.length === 1) [left, right] = [parts[0], parts[0]]
    else if (parts.length === 2) [left, right] = [parts[0], parts[1]]
  }

  if (get('padding-left') !== undefined) left = get('padding-left')
  if (get('padding-right') !== undefined) right = get('padding-right')

  return { left, right }
}

/* Everything that would misplace the card against the list as the two rules
   are written: the list has no horizontal padding, its two sides disagree (one
   left and one right offset cannot match both), or the card steps in by a
   different amount than the list pads. An unresolved var() is reported rather
   than compared, so moving a custom property out of reach cannot pass silently. */
const edgeProblems = (list, card) => {
  const vars = customProps(list)
  const { left, right } = horizontalInsets(list, vars)
  if (left === undefined || right === undefined) return ['the list declares no horizontal padding']

  const problems = []
  for (const [side, expected] of [['left', left], ['right', right]]) {
    if (expected.includes('var(')) {
      problems.push(`the list's ${side} does not resolve: ${expected}`)
      continue
    }
    const actual = resolved(card?.get(side), vars)
    if (actual === undefined) problems.push(`the card declares no ${side} offset`)
    else if (actual.includes('var(')) problems.push(`the card's ${side} does not resolve: ${actual}`)
    else if (actual !== expected) problems.push(`the card's ${side} is ${actual}, the list's is ${expected}`)
  }
  if (left !== right) {
    problems.push(`the list's sides differ (left ${left}, right ${right}); one offset per side cannot match both`)
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
    const problems = edgeProblems(decls('.two-pane-list'), decls('.two-pane-sel'))
    expect(problems, problems.join('; ')).toEqual([])
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

  it('reads padding-inline and lets a lone left or right longhand win', () => {
    expect(horizontalInsets(new Map([['padding-inline', '4px']]))).toEqual({ left: '4px', right: '4px' })
    expect(horizontalInsets(new Map([['padding-inline', '4px 6px']]))).toEqual({ left: '4px', right: '6px' })
    expect(horizontalInsets(new Map([['padding', '0 5px 2px'], ['padding-left', '7px']]))).toEqual({ left: '7px', right: '5px' })
  })

  it('reports a list whose two sides differ, which no one offset can match', () => {
    const list = new Map([['padding', '0 6px 2px 5px']])
    const card = new Map([['left', '5px'], ['right', '5px']])
    expect(edgeProblems(list, card).join('; ')).toContain('sides differ')
  })

  it('reports a card that steps in by a different amount than the list pads', () => {
    const list = new Map([['padding', '0 6px 2px']])
    const card = new Map([['left', '5px'], ['right', '5px']])
    expect(edgeProblems(list, card).join('; ')).toContain("card's left is 5px")
  })
})
