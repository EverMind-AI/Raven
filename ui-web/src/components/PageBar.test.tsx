// @vitest-environment happy-dom
/* The title row every module page starts with: the page's name, what the page
 * offers beside it, and the actions pushed to the far end. */

import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { PageBar } from './PageBar'

afterEach(() => { cleanup() })

describe('the page bar', () => {
  it("names the page in a heading, first in the row", () => {
    const { container } = render(<PageBar title="Task Board"><select aria-label="filter" /></PageBar>)
    const bar = container.querySelector('.page-bar')!
    expect(bar.firstElementChild?.tagName).toBe('H2')
    expect(bar.firstElementChild?.className).toBe('page-bar-title')
    expect(bar.firstElementChild?.textContent).toBe('Task Board')
    expect(bar.children[1]?.tagName).toBe('SELECT')
  })

  it('puts the end actions in a group of their own, last in the row', () => {
    const { container } = render(<PageBar title="Knowledge" end={<button type="button">New</button>}><nav /></PageBar>)
    const bar = container.querySelector('.page-bar')!
    expect(bar.lastElementChild?.className).toBe('page-bar-end')
    expect(bar.lastElementChild?.textContent).toBe('New')
  })

  it('draws no end group when there is nothing to put in it', () => {
    const { container } = render(<PageBar title="Connectors" />)
    expect(container.querySelector('.page-bar-end')).toBeNull()
    expect(container.querySelector('.page-bar')!.children).toHaveLength(1)
  })
})
