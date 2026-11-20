// @vitest-environment happy-dom
/* What the dock's mutation observer answers.
 *
 * It exists for the changes the resize observer cannot see -- a popover over
 * the card opening, a row docking -- and every answer re-measures the dock,
 * which in a browser is a forced layout. So it answers a change, not a write:
 * a class or style written back to the value it already had is no reason to
 * measure, and the field's own height is the field's to lift (store.fitField).
 * Each keystroke writes both kinds, the field's height and the send button's
 * class, so answering them measured the page again on every key.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { _resetForTests, dockChanged, install } from './mount'
import { _resetForTests as storeReset } from './store'

let measured = 0
const settle = (): Promise<void> => new Promise((r) => setTimeout(r, 0))
const $ = (sel: string): HTMLElement => document.querySelector(sel) as HTMLElement

beforeEach(async () => {
  _resetForTests()
  storeReset()
  document.body.innerHTML =
    '<div class="chat"><div class="dock">'
    + '<div class="sheets" id="sheetRack"></div>'
    + '<div class="dock-in">'
    + '<div class="field"><textarea id="ta" rows="1"></textarea></div>'
    + '<div class="under"><button class="go" id="go"></button></div>'
    + '<div class="pop slash" id="slashPop" data-open="false"></div>'
    + '</div></div></div>'
  $('.dock').getClientRects = () => { measured += 1; return [] as unknown as DOMRectList }
  install()
  await settle()
  measured = 0
})

afterEach(() => {
  _resetForTests()
  storeReset()
})

describe('the dock observer', () => {
  it("leaves the field's own height to the field", async () => {
    $('#ta').style.height = 'auto'
    $('#ta').style.height = '70px'
    await settle()
    expect(measured).toBe(0)
  })

  it('does not answer a class written back unchanged', async () => {
    $('#go').classList.remove('halt')
    $('#go').classList.add('go')
    await settle()
    expect(measured).toBe(0)
  })

  it('does not answer an attribute put back within the same batch', async () => {
    $('#slashPop').hidden = true
    $('#slashPop').hidden = false
    await settle()
    expect(measured).toBe(0)
  })

  it('answers a popover opening over the card', async () => {
    $('#slashPop').dataset.open = 'true'
    await settle()
    expect(measured).toBe(1)
  })

  it('answers a row docking above the card', async () => {
    $('#sheetRack').appendChild(document.createElement('div'))
    await settle()
    expect(measured).toBe(1)
  })

  it('answers a class that really changed', async () => {
    $('#go').classList.add('halt')
    await settle()
    expect(measured).toBe(1)
  })
})

/* happy-dom's observer gets two things wrong that the rule depends on: it
   records an old value whether or not one was asked for, and a removed
   attribute's old value comes back null. So the rule is also read here off
   records built by hand, and the request for old values is checked where it
   is made. */
describe('what counts as a change to the dock', () => {
  const record = (target: Element, attributeName: string, oldValue: string | null): MutationRecord =>
    ({ type: 'attributes', target, attributeName, oldValue }) as unknown as MutationRecord

  it('judges an attribute from the value it had before the batch', () => {
    const pop = document.createElement('div')
    expect(dockChanged([record(pop, 'hidden', null), record(pop, 'hidden', '')], null)).toBe(false)
    pop.setAttribute('hidden', '')
    expect(dockChanged([record(pop, 'hidden', null), record(pop, 'hidden', '')], null)).toBe(true)
  })

  it('asks the observer for the old values it judges by', () => {
    _resetForTests()
    const observe = vi.spyOn(MutationObserver.prototype, 'observe')
    install()
    const options = observe.mock.calls.map(([, o]) => o).find((o) => o?.attributeFilter?.includes('data-open'))
    expect(options?.attributeOldValue).toBe(true)
    observe.mockRestore()
  })
})
