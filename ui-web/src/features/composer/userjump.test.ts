// @vitest-environment happy-dom
/* Where the up pill sends the reader: the user turn above the viewport's top
 * edge. Geometry is defined rather than measured -- happy-dom lays nothing out
 * -- so the number asserted here is the scrollTop the pill would write. */

import { afterEach, describe, expect, it } from 'vitest'

import { previousUserTop } from './userjump'

const rect = (top: number): DOMRect => ({
  top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: top,
} as DOMRect)

afterEach(() => {
  document.body.innerHTML = ''
})

function scroller(scrollTop: number): HTMLElement {
  const sc = document.createElement('div')
  sc.id = 'scroll'
  sc.scrollTop = scrollTop
  sc.getBoundingClientRect = () => rect(0)
  document.body.appendChild(sc)
  return sc
}

function turn(sc: HTMLElement, kind: string, top: number): HTMLElement {
  const el = document.createElement('div')
  el.className = `turn ${kind}`
  el.getBoundingClientRect = () => rect(top)
  sc.appendChild(el)
  return el
}

describe('the previous user turn', () => {
  it('takes the last user turn whose top edge is above the viewport', () => {
    const sc = scroller(100)
    turn(sc, 'me', -500)
    turn(sc, 'ai', -300)
    turn(sc, 'me', -50)
    turn(sc, 'ai', 300)
    expect(previousUserTop(sc)).toBe(50)
  })

  it('skips the assistant turns between the questions', () => {
    const sc = scroller(300)
    turn(sc, 'ai', -400)
    turn(sc, 'me', -120)
    turn(sc, 'ai', -60)
    expect(previousUserTop(sc)).toBe(180)
  })

  it('answers null when no question starts above the viewport', () => {
    const sc = scroller(100)
    turn(sc, 'me', 20)
    turn(sc, 'ai', 400)
    expect(previousUserTop(sc)).toBe(null)
  })

  it('answers null when the conversation holds no question at all', () => {
    const sc = scroller(100)
    turn(sc, 'ai', -400)
    expect(previousUserTop(sc)).toBe(null)
  })

  it('clamps the first question to the top of the conversation', () => {
    const sc = scroller(100)
    turn(sc, 'me', -700)
    expect(previousUserTop(sc)).toBe(0)
  })

  it('reads only the turns inside the scroller it was handed', () => {
    const sc = scroller(100)
    turn(sc, 'me', -80)
    const outside = document.createElement('div')
    outside.className = 'turn me'
    outside.getBoundingClientRect = () => rect(-700)
    document.body.appendChild(outside)
    expect(previousUserTop(sc)).toBe(20)
  })

  /* The header floats over the scroller (page.css, .top), so a question just
     below the scroller's own edge is still behind the frost and is the one the
     pill should go back to -- and it must land below the band, not on it. */
  describe('under the frosted header', () => {
    function banded(sc: HTMLElement, height: number): void {
      const chat = document.createElement('div')
      chat.className = 'chat'
      const top = document.createElement('div')
      top.className = 'top'
      Object.defineProperty(top, 'offsetHeight', { configurable: true, value: height })
      document.body.appendChild(chat)
      chat.append(top, sc)
    }

    it('counts a question hidden behind the header as not reached yet', () => {
      const sc = scroller(200)
      banded(sc, 52)
      turn(sc, 'me', 20)
      expect(previousUserTop(sc)).toBe(200 + 20 - 52)
    })

    it('lands the question below the band rather than at the scroller edge', () => {
      const sc = scroller(200)
      banded(sc, 52)
      turn(sc, 'me', -50)
      expect(previousUserTop(sc)).toBe(200 - 50 - 52)
    })
  })
})
