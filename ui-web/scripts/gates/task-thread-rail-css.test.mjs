/* Where a task thread draws its vertical lines.
 *
 * A thread's events hang on one rail in the marks' column: the grey line that
 * joins one event's mark to the next, and the amber one under the event its
 * group waits on, beside that event's summary. The summary block draws no edge
 * of its own -- one inset to the text column reads as the rail breaking off and
 * starting again further right. happy-dom applies no stylesheet, so the column
 * is pinned here.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { decls, rules } from './css.mjs'

const tasks = readFileSync(new URL('../../src/features/tasks/styles.css', import.meta.url), 'utf8')

/* Every rule whose subject -- the last compound of a selector, after any
   descendant, child or sibling combinator -- is the step element itself, as
   `[selector, body]` pairs.

   The subject is what decides which box a rule's declarations land on, so this
   is the set that can open a gap between one step's box and the next: `.tkstep`
   itself, `.tkstep:not(:last-child)`, `.tkstep + .tkstep`, and any sibling
   selector a later change adds. Rules whose subject is `::after` or a
   descendant such as `.tkmark` style something inside the step, not the box the
   rail is positioned in, so they cannot break it. Reading by subject rather
   than by one hard-coded selector is the point: the margin that broke this
   rail was on the base `.tkstep` rule, which a lookup of a single exact
   selector never saw. */
const stepBox = rules(null, tasks).filter(([selector]) => selector.split(',').some((part) => {
  const subject = part.trim().split(/[\s>+~]+/).pop() || ''
  return /\.tkstep(?:$|[:[.])/.test(subject) && !subject.includes('::')
}))

/* A rule body as `[property, value]` pairs. */
const declarations = (body) => body
  .split(';')
  .map((decl) => {
    const at = decl.indexOf(':')
    return at > 0 ? [decl.slice(0, at).trim(), decl.slice(at + 1).trim()] : null
  })
  .filter(Boolean)

/* The margin longhands that move a box up or down. `margin-inline` does not,
   and is left out on purpose. */
const VERTICAL_MARGIN = /^margin(?:-(?:top|bottom|block|block-start|block-end))?$/

describe('a task thread\'s rail', () => {
  it('runs in the marks\' column under the event a group waits on, as it does between events', () => {
    const between = decls('.tkstep:not(:last-child)::after', tasks)
    const waits = decls('.tkstep[data-asks]::after', tasks)
    expect(waits).not.toBeNull()
    for (const prop of ['position', 'left', 'top', 'bottom', 'width']) expect(waits.get(prop)).toBe(between.get(prop))
    expect(waits.get('background')).toBe('var(--amber)')
  })

  it('leaves the waiting event\'s summary without an edge of its own', () => {
    const box = decls('.tkstepsum', tasks)
    expect(['border', 'border-left'].filter((prop) => box.has(prop))).toEqual([])
  })

  /* The rail is one unbroken line: it starts where a mark ends and ends where
     the next mark begins. Both ends are the mark's own geometry rather than a
     length the text can change. */
  it('spans from a mark\'s lower edge to the next mark\'s upper edge', () => {
    const px = (value) => Number.parseFloat(value)
    const mark = decls('.tkmark', tasks)
    const rail = decls('.tkstep:not(:last-child)::after', tasks)
    expect(rail.get('top')).toBe(`${px(mark.get('top')) + px(mark.get('height'))}px`)
    expect(rail.get('bottom')).toBe(`${-px(mark.get('top'))}px`)
  })

  /* A pseudo element paints inside its own box, so a vertical margin on the
     step is space the rail cannot cross: it is what left each event's line
     stopping short of the next. Every rule that styles the step box has to be
     free of one, not just the rule carrying the rail -- the regression this
     gate guards was a margin on the base `.tkstep` rule and another on
     `.tkstep + .tkstep`. */
  it('keeps every rule that styles the step box free of a vertical margin', () => {
    /* Reading nothing would satisfy the assertion below for the wrong reason,
       so the base rule -- the one whose margin broke the rail -- must be in the
       set the scan actually saw. `.tksteps` (the list) must not be: it is a
       different box, and its own margin: 0 is fine. */
    const subjects = stepBox.map(([selector]) => selector)
    expect(subjects).toContain('.tkstep')
    expect(subjects).not.toContain('.tksteps')

    const margins = stepBox.flatMap(([selector, body]) =>
      declarations(body)
        .filter(([prop]) => VERTICAL_MARGIN.test(prop))
        .map(([prop, value]) => `${selector} { ${prop}: ${value} }`))
    expect(margins).toEqual([])
  })
})
