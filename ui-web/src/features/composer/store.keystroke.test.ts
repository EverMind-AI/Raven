// @vitest-environment happy-dom
/* What a keystroke costs in layout.
 *
 * The field is sized from its content on every input, and that one read of
 * `scrollHeight` is a forced layout no textarea can avoid. What follows it is
 * not owed every time: the dock is re-measured only when the field's height
 * actually moved, and when it is, every read comes before the first write, so
 * the measurement does not lay the page out a second time. Nor is the send
 * button's icon redrawn while it stays the same icon: a rewrite is a change
 * inside the dock, which its observer answers by measuring the page.
 *
 * happy-dom does no layout, so the cost is counted as the reads the code asks
 * for: a call to the dock's own geometry is a forced layout in a browser.
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { _resetForTests, dockLift, fitField, goPaint, resizeField } from './store'

let content = 60
let measured = 0
const order: string[] = []

const rect = (top: number, height: number): DOMRect => ({
  top, height, bottom: top + height, left: 0, right: 300, width: 300, x: 0, y: top, toJSON: () => ({}),
}) as DOMRect

beforeEach(() => {
  _resetForTests()
  document.body.innerHTML =
    '<div class="chat"><div class="dock"><div class="dock-in">'
    + '<div class="field"><textarea id="ta" rows="1"></textarea></div>'
    + '<button class="go" id="go"></button>'
    + '</div></div></div>'
  content = 60
  measured = 0
  order.length = 0
  const chat = document.querySelector('.chat') as HTMLElement
  const dock = document.querySelector('.dock') as HTMLElement
  const ta = document.getElementById('ta') as HTMLTextAreaElement
  Object.defineProperty(ta, 'scrollHeight', { configurable: true, get: () => content })
  chat.getBoundingClientRect = () => rect(0, 600)
  dock.getClientRects = () => { measured += 1; return [rect(460, 140)] as unknown as DOMRectList }
  dock.getBoundingClientRect = () => rect(460, 140)
  ;(document.querySelector('.dock-in') as HTMLElement).getBoundingClientRect = () => rect(500, 100)
  ta.getBoundingClientRect = () => { order.push('read field'); return rect(520, content) }
  const set = chat.style.setProperty.bind(chat.style)
  chat.style.setProperty = (name: string, value: string | null) => { order.push(`publish ${name}`); set(name, value) }
})

afterEach(() => { _resetForTests() })

describe('a keystroke in the composer', () => {
  it('re-measures the dock when the field grew or shrank', () => {
    fitField()
    expect(measured).toBe(1)
    content = 80
    fitField()
    expect(measured).toBe(2)
    content = 40
    fitField()
    expect(measured).toBe(3)
  })

  it('leaves the dock alone when the field kept its height', () => {
    fitField()
    measured = 0
    fitField()
    fitField()
    expect(measured).toBe(0)
  })

  it('leaves the dock alone when a dragged height holds', () => {
    resizeField(120)
    measured = 0
    content = 90
    fitField()
    expect(measured).toBe(0)
  })
})

describe('the dock lift', () => {
  it('reads the field before it publishes anything, so the page is laid out once', () => {
    dockLift()
    expect(order).toContain('publish --lift')
    expect(order.indexOf('read field')).toBeGreaterThanOrEqual(0)
    expect(order.indexOf('read field')).toBeLessThan(order.indexOf('publish --lift'))
  })
})

describe('the send button on a keystroke', () => {
  it('keeps the icon it already draws', async () => {
    goPaint()
    const icon = document.querySelector('#go svg')
    expect(icon).not.toBeNull()
    let rewrites = 0
    new MutationObserver((records) => { rewrites += records.length })
      .observe(document.getElementById('go')!, { childList: true, subtree: true })
    ;(document.getElementById('ta') as HTMLTextAreaElement).value = 'a'
    goPaint()
    ;(document.getElementById('ta') as HTMLTextAreaElement).value = 'ab'
    goPaint()
    await new Promise((r) => setTimeout(r, 0))
    expect(rewrites).toBe(0)
    expect(document.querySelector('#go svg')).toBe(icon)
  })
})
