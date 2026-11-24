// @vitest-environment happy-dom
/* The card that marks the selected row, and the sliding it does.
 *
 * happy-dom does no layout, so a row's box is placed by the test here: the
 * card's whole job is to sit where the row is, and the row's box is the only
 * thing it knows.
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { TwoPaneList, TwoPaneRow } from './TwoPane'

/* Where the row the card is on sits, and how tall it is. */
let box = { top: 0, height: 0 }

const place = (top: number, height: number): void => {
  box = { top, height }
  Object.defineProperty(HTMLElement.prototype, 'offsetTop', { configurable: true, get: () => box.top })
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get: () => box.height })
}

const card = (): HTMLElement | null => document.querySelector<HTMLElement>('.two-pane-sel')

afterEach(() => {
  cleanup()
  /* The stub is on the prototype, so it has to come off again. */
  delete (HTMLElement.prototype as unknown as Record<string, unknown>).offsetTop
  delete (HTMLElement.prototype as unknown as Record<string, unknown>).offsetHeight
})

describe('the card that marks the selected row', () => {
  it('is drawn on the row the list says is current', () => {
    place(42, 40)
    render(
      <TwoPaneList>
        <TwoPaneRow current={false} name="first" onOpen={() => {}} />
        <TwoPaneRow current name="second" onOpen={() => {}} />
      </TwoPaneList>,
    )
    expect(card()).not.toBeNull()
    expect(card()?.style.transform).toBe('translateY(42px)')
    expect(card()?.style.height).toBe('40px')
  })

  it('moves to the row that is selected next', () => {
    place(42, 40)
    const { rerender } = render(
      <TwoPaneList>
        <TwoPaneRow current name="first" onOpen={() => {}} />
        <TwoPaneRow current={false} name="second" onOpen={() => {}} />
      </TwoPaneList>,
    )
    expect(card()?.style.transform).toBe('translateY(42px)')
    /* The next row is further down the list. */
    place(126, 40)
    rerender(
      <TwoPaneList>
        <TwoPaneRow current={false} name="first" onOpen={() => {}} />
        <TwoPaneRow current name="second" onOpen={() => {}} />
      </TwoPaneList>,
    )
    expect(card()?.style.transform).toBe('translateY(126px)')
  })

  it('follows the row when the list moves it under the same selection', () => {
    place(42, 40)
    /* The rows arrive as a list, the way the pages that draw this frame hand
       them over: `items.map(...)` is an array even when it holds one row. */
    const rows = (names: string[], current: number) => names.map((n, i) => (
      <TwoPaneRow key={n} current={i === current} name={n} onOpen={() => {}} />
    ))
    const { rerender } = render(<TwoPaneList>{rows(['first', 'second'], 0)}</TwoPaneList>)
    expect(card()?.style.transform).toBe('translateY(42px)')
    /* The row is the same row afterwards: the card must not renumber the
       list, or every selection would rebuild the rows under it. */
    const before = document.querySelector('.two-pane-row')
    /* The same row is still selected, but the row above it went away (a
       search narrowed the list): its own box moved and nothing about the
       selection changed. */
    place(0, 40)
    rerender(<TwoPaneList>{rows(['first'], 0)}</TwoPaneList>)
    expect(document.querySelector('.two-pane-row')).toBe(before)
    expect(card()?.style.transform).toBe('translateY(0px)')
  })

  it('takes the card away when the selected row leaves the list', () => {
    place(42, 40)
    const rows = (names: string[], current: string | null) => names.map((n) => (
      <TwoPaneRow key={n} current={n === current} name={n} onOpen={() => {}} />
    ))
    const { rerender } = render(<TwoPaneList>{rows(['first', 'second'], 'first')}</TwoPaneList>)
    expect(card()).not.toBeNull()
    /* The row the card is on is gone; the row left behind is not selected, so
       the card must not stay behind on it. */
    rerender(<TwoPaneList>{rows(['second'], null)}</TwoPaneList>)
    expect(card()).toBeNull()
  })

  it('keeps the card when a row under the selected one leaves', () => {
    place(42, 40)
    const rows = (names: string[], current: string | null) => names.map((n) => (
      <TwoPaneRow key={n} current={n === current} name={n} onOpen={() => {}} />
    ))
    const { rerender } = render(<TwoPaneList>{rows(['first', 'second', 'third'], 'first')}</TwoPaneList>)
    expect(card()?.style.transform).toBe('translateY(42px)')
    /* A row below the selected one leaves. Its unmount reports that it has no
       box, and only the row the card is on may take the card away: the
       selected row did not move, so it has nothing new to report. */
    rerender(<TwoPaneList>{rows(['first', 'second'], 'first')}</TwoPaneList>)
    expect(card()).not.toBeNull()
    expect(card()?.style.transform).toBe('translateY(42px)')
  })

  it('is not drawn when nothing is selected', () => {
    place(42, 40)
    render(
      <TwoPaneList>
        <TwoPaneRow current={false} name="first" onOpen={() => {}} />
        <TwoPaneRow current={false} name="second" onOpen={() => {}} />
      </TwoPaneList>,
    )
    expect(card()).toBeNull()
  })
})
