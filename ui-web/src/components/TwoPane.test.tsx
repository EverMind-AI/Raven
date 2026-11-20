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
