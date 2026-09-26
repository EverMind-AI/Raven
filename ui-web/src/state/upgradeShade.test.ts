// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import { mountPageRoot } from '../test/pageRoot'
import * as confirmStore from './confirm'
import * as pageStore from './page'
import { _resetForTests, open } from './upgradeShade'

/* The card is drawn by src/chrome/UpgradeShade.tsx, so the page's own root has
   to be standing for one to reach the body (see src/main.tsx). */
let unmount = (): void => {}

function wire(): void {
  setTranslator(key => key)
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
}

beforeEach(() => {
  unmount = mountPageRoot()
})

afterEach(() => {
  /* The card is never taken down in the page, so the store outlives a case. */
  _resetForTests()
  unmount()
  resetTranslator()
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('the upgrade shade writer', () => {
  it('replaces an older shade and draws the legacy waiting shape', () => {
    wire()
    document.body.innerHTML = '<div class="upshade">old</div><div id="keep"></div>'
    const shade = open()
    shade.say('Working')
    expect(document.body.innerHTML).toBe(
      '<div id="keep"></div><div class="upshade"><div class="upcard"><div class="upbar"><i></i></div><div class="t">Working</div></div></div>'
    )
  })

  it('shows the failure affordances, copies the command, and closes', () => {
    wire()
    const writeText = vi.fn()
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const shade = open()
    shade.fail('Failed', 'disk full')
    expect(document.querySelector('.upcard')!.innerHTML).toBe(
      '<div class="upbar" hidden=""><i></i></div><div class="t">Failed</div>'
      + '<div class="sub">disk full\ngui.upg.manual</div>'
      + '<div class="cmd"><code>raven upgrade</code><button>gui.dtl.copy</button></div>'
      + '<div class="foot"><button class="btn">gui.upg.close</button></div>'
    )
    ;(document.querySelector('.cmd button') as HTMLButtonElement).click()
    expect(writeText).toHaveBeenCalledWith('raven upgrade')
    ;(document.querySelector('.foot button') as HTMLButtonElement).click()
    expect(document.querySelector('.upshade')).toBeNull()
  })

  it('closes only the node represented by its handle', () => {
    wire()
    const first = open()
    const old = document.querySelector('.upshade')!
    old.classList.add('old')
    const second = open()
    const current = document.querySelector('.upshade')!
    document.body.appendChild(old)
    first.close()
    expect(current.isConnected).toBe(true)
    second.close()
    expect(current.isConnected).toBe(false)
  })
})

/* The bar slides until the helper reports bytes, measures them while it does,
   and slides again when there is nothing to count -- uv resolving, the new
   Raven starting. A bar that kept its last width then would be lying. */
describe('the measured upgrade bar', () => {
  it('measures what the helper reports', () => {
    wire()
    const shade = open()
    shade.measure('Downloading', 0.5)
    const bar = document.querySelector('.upshade .upbar') as HTMLElement
    expect(bar.hasAttribute('data-measured')).toBe(true)
    expect((bar.querySelector('i') as HTMLElement).style.width).toBe('50.0%')
    expect(document.querySelector('.upshade .t')!.textContent).toBe('Downloading')
  })

  it('goes back to sliding when nothing can be measured', () => {
    wire()
    const shade = open()
    shade.measure('Downloading', 0.9)
    shade.measure('Installing', null)
    const bar = document.querySelector('.upshade .upbar') as HTMLElement
    expect(bar.hasAttribute('data-measured')).toBe(false)
    expect((bar.querySelector('i') as HTMLElement).getAttribute('style')).toBeNull()
  })

  it('never draws past either end', () => {
    wire()
    const shade = open()
    shade.measure('Downloading', 1.7)
    expect((document.querySelector('.upshade .upbar i') as HTMLElement).style.width).toBe('100.0%')
  })
})
