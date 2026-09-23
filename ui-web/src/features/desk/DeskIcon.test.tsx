/** Tests for the desk's shared tab/pane glyphs. */

// @vitest-environment happy-dom
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { DeskIcon } from './DeskIcon'

const svgOf = (el: HTMLElement): string => el.querySelector('svg')?.innerHTML ?? ''

describe('DeskIcon', () => {
  /* The prototype keeps one glyph, `ICONS.bot`, on the tasks tab, its empty
     state AND the pane a delegated run opens as (proto.js:4355, :4478); a
     tab and the pane it opens reading as two different things would say the
     tab lied about what it opens. */
  it('draws the same glyph for the tasks tab and an agent pane', () => {
    const tasks = render(<DeskIcon kind="tasks" />)
    const agents = render(<DeskIcon kind="agents" />)
    expect(svgOf(tasks.container)).toBe(svgOf(agents.container))
    expect(svgOf(tasks.container)).not.toBe('')
  })

  it('does not draw the tasks tab as the step-graph shape the other domains use for a DAG', () => {
    const { container } = render(<DeskIcon kind="tasks" />)
    /* `ICONS.task`, proto.js:3890 -- two offset squares joined by an elbow --
       is a real glyph in the prototype, just never the one this tab wears. */
    expect(container.querySelector('rect[width="7"]')).toBeNull()
  })

  it('keeps every other tab its own glyph', () => {
    const kinds = ['deliverables', 'diff', 'file'] as const
    const drawn = kinds.map((kind) => {
      const { container } = render(<DeskIcon kind={kind} />)
      return svgOf(container)
    })
    expect(new Set(drawn).size).toBe(kinds.length)
    for (const svg of drawn) expect(svg).not.toBe(svgOf(render(<DeskIcon kind="tasks" />).container))
  })
})
