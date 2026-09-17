// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import { clearance } from './popover'
import { mountPageRoot } from '../test/pageRoot'

/* happy-dom measures every box as zero, so each element that matters here is
   handed the rect it would have on a laid-out page. The numbers are the ones
   measured on the running composer: the card at 436..562, the chip on its bottom
   bar at 518..539. */
function rect(el: Element, top: number, bottom: number): void {
  el.getBoundingClientRect = () =>
    ({ top, bottom, left: 0, right: 0, width: 0, height: bottom - top, x: 0, y: top } as DOMRect)
}

let unmount = (): void => {}

afterEach(() => {
  unmount()
  unmount = () => {}
  document.body.innerHTML = ''
})

describe('what an anchored panel has to clear', () => {
  it('answers with the composer card when the anchor is a chip on it', () => {
    document.body.innerHTML = `
      <div class="dock-in">
        <div class="field"><textarea></textarea></div>
        <div class="under"><button class="chip" id="permChip"></button></div>
      </div>`
    const card = document.querySelector('.dock-in')!
    const chip = document.getElementById('permChip')!
    rect(card, 436, 562)
    rect(chip, 518, 539)
    const over = clearance(chip)
    /* The card's edges, not the chip's. Raised off the chip, a panel's lower
       edge lands at 518 -- eighty-odd pixels INSIDE the card, over the field
       the reader is typing in, which is the bug this answers. */
    expect(over.top).toBe(436)
    expect(over.bottom).toBe(562)
  })

  it('answers with the anchor itself when it is not on a card', () => {
    /* The model picker also opens from a settings row, which floats on the page
       and has nothing around it to clear. */
    document.body.innerHTML = '<div class="setting"><button id="modelChip"></button></div>'
    const anchor = document.getElementById('modelChip')!
    rect(anchor, 200, 224)
    expect(clearance(anchor).top).toBe(200)
    expect(clearance(anchor).bottom).toBe(224)
  })

  it('names a class the composer actually wears', () => {
    /* The selector is a string here and the card is an element the page root
       renders over there, so nothing but this ties them together: rename the
       wrapper in src/chrome/Dock.tsx and every panel silently goes back to
       clearing its chip -- which is a layout regression no type-checker and no
       other test can see. Asserted on the element that holds the composer's own
       field, not on a bare grep, so a `.dock-in` appearing anywhere else would
       not satisfy it. */
    document.body.innerHTML = '<div class="dock"></div>'
    unmount = mountPageRoot()
    const card = document.querySelector('.dock-in')
    expect(card).not.toBeNull()
    expect(card!.querySelector('#ta')).not.toBeNull()
    expect(card!.querySelector('#permChip')).not.toBeNull()
    expect(card!.querySelector('#modelChip')).not.toBeNull()
    expect(card!.querySelector('#tierChip')).not.toBeNull()
  })

  it('answers with the card when the anchor IS the card', () => {
    /* `closest` matches the element itself, and that is the right answer: a
       panel anchored on the card clears the card. */
    document.body.innerHTML = '<div class="dock-in" id="card"></div>'
    const card = document.getElementById('card')!
    rect(card, 436, 562)
    expect(clearance(card).top).toBe(436)
  })
})
