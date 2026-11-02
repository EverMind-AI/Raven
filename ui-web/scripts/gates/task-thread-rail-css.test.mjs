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

import { decls } from './css.mjs'

const tasks = readFileSync(new URL('../../src/features/tasks/styles.css', import.meta.url), 'utf8')

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
})
