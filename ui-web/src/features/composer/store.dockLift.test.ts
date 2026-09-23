// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'

import { dockLift } from './store'

/* What --lift has to be worth: the distance from the chat column's bottom edge
 * up to the top of the highest thing docked over it. The chat reserves it as
 * bottom padding and fades its tail from it (styles/page.css), so anything
 * docked that this misses is something that covers the transcript instead of
 * moving it up. The running-task chip did exactly that -- it sits above the
 * composer card, and the measurement named the card's classes rather than
 * counting what is docked.
 *
 * happy-dom does no layout and reports every rect as zero, so the rects are
 * stubbed per element; the numbers are the only thing being asserted, and a
 * presence-only check would pass with the chip still unmeasured.
 */
const CHAT_BOTTOM = 600

function box(el: Element, top: number, height: number): void {
  el.getBoundingClientRect = () => ({
    top, height, bottom: top + height, left: 0, right: 0, width: 300, x: 0, y: top, toJSON: () => ({}),
  }) as DOMRect
}

const lift = (): string => (document.querySelector('.chat') as HTMLElement).style.getPropertyValue('--lift')

beforeEach(() => {
  document.body.innerHTML =
    '<div class="chat"><div class="dock">'
    + '<div class="sheets" id="sheetRack"></div>'
    + '<div class="tkruns"><button class="tkrunhint"></button></div>'
    + '<div class="dock-in"></div>'
    + '</div></div>'
  box(document.querySelector('.chat')!, 0, CHAT_BOTTOM)
  box(document.querySelector('.dock')!, 460, 140)
  box(document.querySelector('.dock-in')!, 500, 100)
}) 

describe('the dock lift', () => {
  it('reaches the top of the composer card when nothing else is docked', () => {
    box(document.querySelector('.tkruns')!, 0, 0)
    box(document.querySelector('.sheets')!, 0, 0)
    dockLift()
    expect(lift()).toBe('100px')
  })

  it('reaches over the running-task chip, so the transcript moves up by its height', () => {
    box(document.querySelector('.tkruns')!, 470, 26)
    box(document.querySelector('.sheets')!, 0, 0)
    dockLift()
    expect(lift()).toBe('130px')
  })

  it('reaches over a sheet stacked above both', () => {
    box(document.querySelector('.tkruns')!, 470, 26)
    box(document.querySelector('.sheets')!, 400, 60)
    dockLift()
    expect(lift()).toBe('200px')
  })

  it('ignores a docked thing with no height, which is how an empty sheet rack reads', () => {
    box(document.querySelector('.tkruns')!, 0, 0)
    box(document.querySelector('.sheets')!, 0, 0)
    dockLift()
    expect(lift()).toBe('100px')
  })
})
