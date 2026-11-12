/* Where the record's action row hangs, read off the tasks sheet.
 *
 * The conversation draws a turn's actions a 5px negative margin outside the
 * card they belong to, mirrored to the side the card is aligned to. The chat
 * gives that overhang a gutter (the reading column's padding) and the agent
 * stage gives it one too (.satx), so nothing leaves the box. The record --
 * the subagent's own conversation, drawn by the same renderer inside the task
 * pane -- had neither: its body is a scroll box flush with its cards, so the
 * row widened the column by 5px and the body answered with 5px of sideways
 * scroll, a strip at the bottom of the dialog a reader could drag the record
 * sideways through.
 *
 * The gutter is the fix, and it is a number in a stylesheet, so it is pinned
 * here: happy-dom lays nothing out and no DOM test can see it.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

const sheet = readFileSync(new URL('../../src/features/tasks/styles.css', import.meta.url), 'utf8')

describe('the record column', () => {
  it('keeps the gutter the hanging action row needs', () => {
    expect(decls('.tkrecord', sheet).get('padding')).toBe('6px 5px 8px')
  })

  it('hangs no row of its own outside that gutter', () => {
    expect(decls('.tkrecord > [data-tsl] > *', sheet).get('margin')).toBeUndefined()
  })
})
