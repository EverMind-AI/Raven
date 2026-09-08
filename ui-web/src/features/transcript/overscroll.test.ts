/** What a nested stream box does with a wheel it can no longer answer.
 *
 * Geometry is fabricated rather than laid out: happy-dom lays nothing out, so
 * `scrollHeight` and `clientHeight` are both 0 there and every box would look
 * unscrollable. Defining them is what makes the walk answerable at all; the
 * scroll POSITION is then only ever the number the code under test wrote, which
 * is exactly what these assert.
 */

// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import { WHEEL_LINE_PX, isUpwardOverscroll, pageScroller, releaseUpward } from './overscroll'

function sized(el: HTMLElement, scrollHeight: number, clientHeight: number): HTMLElement {
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true })
  return el
}

/* The shape the card is mounted in: a scrolling page, the card, and the stream
   box inside it. */
function tree(pageOverflow = 'auto'): { page: HTMLElement; box: HTMLElement } {
  document.body.innerHTML = '<div id="page"><div class="card"><div class="box"></div></div></div>'
  const page = document.querySelector('#page') as HTMLElement
  const box = document.querySelector('.box') as HTMLElement
  page.style.overflowY = pageOverflow
  sized(page, 4000, 900)
  sized(box, 900, 340)
  page.scrollTop = 1000
  box.scrollTop = 0
  return { page, box }
}

afterEach(() => { document.body.innerHTML = '' })

describe('an upward overscroll out of a contained box', () => {
  it('is only the wheel the box itself cannot answer', () => {
    /* Both halves matter: a box with room left is still the right target, and a
       downward wheel at the top is the box's own to take. */
    expect(isUpwardOverscroll({ scrollTop: 0 }, -120)).toBe(true)
    expect(isUpwardOverscroll({ scrollTop: 200 }, -120)).toBe(false)
    expect(isUpwardOverscroll({ scrollTop: 0 }, 120)).toBe(false)
  })

  it('moves the page when the box is already at its top', () => {
    const { page, box } = tree()

    expect(releaseUpward(box, -120)).toBe(true)
    expect(page.scrollTop).toBe(880)
  })

  it('reads a line-mode wheel in lines, not pixels', () => {
    /* Only mode 0 is pixels. A mouse reporting lines sends about -3 for one
       notch; taken as pixels the page would creep 3px while the box goes on
       eating the gesture -- the same freeze, quieter. */
    const { page, box } = tree()

    expect(releaseUpward(box, -3, 1)).toBe(true)
    expect(page.scrollTop).toBe(1000 - 3 * WHEEL_LINE_PX)
  })

  it('reads a page-mode wheel as the scroller own height', () => {
    /* A page means a page OF THE THING BEING SCROLLED, so it is the scroller's
       visible height and not the box's or the window's. */
    const { page, box } = tree()

    expect(releaseUpward(box, -1, 2)).toBe(true)
    expect(page.scrollTop).toBe(1000 - 900)
  })

  it('falls back to one line when a page-mode scroller reports no height', () => {
    /* Multiplying by zero would report a move and make none, which reads as the
       trap still being there. */
    const { page, box } = tree()
    Object.defineProperty(page, 'clientHeight', { value: 0, configurable: true })
    Object.defineProperty(page, 'scrollHeight', { value: 4000, configurable: true })

    expect(releaseUpward(box, -1, 2)).toBe(true)
    expect(page.scrollTop).toBe(1000 - WHEEL_LINE_PX)
  })

  it('leaves the page alone while the box still has room', () => {
    /* Containment is not the thing being removed. A box mid-scroll answers its
       own wheel, and forwarding as well would move two scrollers at once. */
    const { page, box } = tree()
    box.scrollTop = 200

    expect(releaseUpward(box, -120)).toBe(false)
    expect(page.scrollTop).toBe(1000)
  })

  it('leaves the page alone on the way down, which is what contain is for', () => {
    /* The downward edge keeps the CSS containment: reading a stream to its end
       must not then carry the conversation past the card. Nothing here should
       hand that gesture on. */
    const { page, box } = tree()

    expect(releaseUpward(box, 120)).toBe(false)
    expect(page.scrollTop).toBe(1000)
  })

  it('walks past an ancestor that does not scroll', () => {
    /* `.card` is between the box and the page and is not a scroller. Stopping
       at the first parent would forward into an element with nowhere to go. */
    const { page, box } = tree()
    const card = document.querySelector('.card') as HTMLElement
    sized(card, 900, 900)

    expect(pageScroller(box)).toBe(page)
  })

  it('finds no scroller when the page does not scroll, and moves nothing', () => {
    /* A conversation short enough to fit has no page scroll to hand the gesture
       to, and inventing one would be a jump out of nowhere. */
    const { page, box } = tree('visible')

    expect(pageScroller(box)).toBeNull()
    expect(releaseUpward(box, -120)).toBe(false)
    expect(page.scrollTop).toBe(1000)
  })

  it('finds no scroller in an ancestor whose content already fits', () => {
    /* `overflow-y: auto` on a box nothing overflows is not a scroller: it draws
       no bar and cannot move, so forwarding to it would swallow the gesture the
       same way the box did. */
    const { box } = tree()
    const page = document.querySelector('#page') as HTMLElement
    sized(page, 900, 900)

    expect(pageScroller(box)).toBeNull()
  })
})
