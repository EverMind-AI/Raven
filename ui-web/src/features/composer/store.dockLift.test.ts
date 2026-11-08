// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

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
const restLift = (): string => (document.querySelector('.chat') as HTMLElement).style.getPropertyValue('--lift-rest')

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

  it('writes nothing when nothing moved, since it also runs on every scroll event', () => {
    box(document.querySelector('.tkruns')!, 0, 0)
    box(document.querySelector('.sheets')!, 0, 0)
    dockLift()
    const chat = document.querySelector('.chat') as HTMLElement
    const writes = vi.spyOn(chat.style, 'setProperty')
    dockLift()
    expect(writes).not.toHaveBeenCalled()
    box(document.querySelector('.dock-in')!, 480, 120)
    dockLift()
    expect(writes).toHaveBeenCalledWith('--lift', '120px')
  })

  it('ignores a docked thing with no height, which is how an empty sheet rack reads', () => {
    box(document.querySelector('.tkruns')!, 0, 0)
    box(document.querySelector('.sheets')!, 0, 0)
    dockLift()
    expect(lift()).toBe('100px')
  })

  it('lifts nothing when the dock is not drawn, as on an imported conversation', () => {
    const dock = document.querySelector('.dock') as HTMLElement
    box(dock, 0, 0)
    dock.getClientRects = () => ({ length: 0, item: () => null, [Symbol.iterator]: [][Symbol.iterator] }) as unknown as DOMRectList
    dockLift()
    expect(lift()).toBe('0px')
    expect(restLift()).toBe('0px')
  })
})

/* The resting lift is the same distance with the field at its resting height:
 * what the turn nav stands on (features/transcript/styles.css), so a field
 * that grows -- dragged taller, or filled with text -- moves the transcript's
 * edge and leaves the nav where it was. Everything else docked still counts.
 */
describe('the resting lift', () => {
  it('takes the field\'s growth out of the lift and nothing else', () => {
    const card = document.querySelector('.dock-in') as HTMLElement
    card.innerHTML = '<div class="field"><textarea id="ta"></textarea></div>'
    const ta = document.getElementById('ta') as HTMLTextAreaElement
    ta.style.minHeight = '52px'
    box(ta, 420, 152)
    box(card, 400, 200)
    box(document.querySelector('.tkruns')!, 0, 0)
    box(document.querySelector('.sheets')!, 300, 60)
    dockLift()
    expect(lift()).toBe('300px')
    expect(restLift()).toBe('200px')
  })

  it('counts a field that is not drawn as grown by nothing, as in the trajectory view', () => {
    const card = document.querySelector('.dock-in') as HTMLElement
    card.innerHTML = '<div class="field"><textarea id="ta"></textarea></div>'
    const ta = document.getElementById('ta') as HTMLTextAreaElement
    ta.style.minHeight = '52px'
    box(ta, 0, 0)
    box(card, 0, 0)
    box(document.querySelector('.tkruns')!, 0, 0)
    box(document.querySelector('.sheets')!, 300, 60)
    dockLift()
    expect(lift()).toBe('300px')
    expect(restLift()).toBe('300px')
  })
})
