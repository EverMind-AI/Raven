// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Shell } from './bridge'
import { open } from './upgrade'

function wire(): void {
  const shell: Shell = {
    T: key => key,
    confirmAsk: () => {},
    showPage: () => {},
  }
  window.RavenShell = shell
}

afterEach(() => {
  delete window.RavenShell
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
