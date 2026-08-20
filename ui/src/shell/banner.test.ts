// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import { draw, setFault } from './banner'

import type { BannerSource } from './banner'
import type { Shell } from './bridge'

interface Wired {
  opened: number
}

function wire(needsWebsearch = false, withSource = true): Wired {
  const w: Wired = { opened: 0 }
  const shell: Shell = {
    T: (key) => key,
    toast: () => {},
    menuAt: () => {},
    confirmAsk: () => {},
    showPage: () => {},
    openWebsearch: () => {
      w.opened += 1
    },
  }
  window.RavenShell = shell
  const source: BannerSource = { websearchNeeds: () => needsWebsearch }
  window.DS = withSource ? { banner: source } : {}
  document.body.innerHTML = '<div id="bannerHost"></div>'
  return w
}

const host = (): HTMLElement => document.getElementById('bannerHost')!
const banner = (): HTMLElement | null => host().querySelector('.banner')

afterEach(() => {
  setFault(null)
  delete window.RavenShell
  delete window.DS
  document.body.innerHTML = ''
})

describe('the banner strip', () => {
  it('shows nothing when there is nothing to say', () => {
    wire(false)
    draw()
    expect(host().children.length).toBe(0)
  })

  it('draws the memory fault with its detail, and no way to dismiss it', () => {
    wire(false)
    setFault('disk full')
    draw()
    const b = banner()!
    expect(b.className).toBe('banner bad')
    expect(b.querySelector('b')!.textContent).toBe('gui.mem.down')
    expect(b.querySelector('span')!.textContent).toBe('disk full')
    /* The condition lasts until it is fixed; a dismissable one gets dismissed
       and then forgotten. */
    expect(b.querySelector('button')).toBeNull()
  })

  /* The whole bug this fixes, at the level a unit test can reach it: a setter
     whose effect waited on the caller making a second call. The live layer's
     one caller made it, and the name it called had been replaced with a clear,
     so the fault was stored and never seen. */
  it('puts the fault on screen by itself, and takes it away by itself', () => {
    wire(false)
    setFault('disk full')
    expect(banner()).toBeTruthy()
    setFault(null)
    expect(banner()).toBeNull()
  })

  /* Deliberately not a second test for "a source that refuses the suggestion
     still gets the fault": every mutation that would break it breaks the one
     above, since that one already wires a refusing source. The live half of
     this fix -- the source replacing the drawing override -- is in a concat
     layer vitest does not load, and is verified in a browser instead. */

  it('offers the websearch notice when the capability is unconfigured', () => {
    const w = wire(true)
    draw()
    const b = banner()!
    expect(b.className).toBe('banner')
    const labels = [...b.querySelectorAll('button')].map((x) => x.textContent)
    expect(labels.length).toBe(2)
    ;(b.querySelector('button') as HTMLElement).click()
    expect(w.opened).toBe(1)
  })

  it('lets the reader wave the suggestion away', () => {
    wire(true)
    draw()
    const b = banner()!
    const x = b.querySelector('button.x') as HTMLElement
    expect(x.getAttribute('aria-label')).toBeTruthy()
    x.click()
    expect(banner()).toBeNull()
  })

  /* Order is the design: a backend that stopped storing has been handing back
     normal-looking replies, while an unconfigured search visibly refuses. */
  it('lets the fault win over the suggestion, alone', () => {
    wire(true)
    setFault('disk full')
    draw()
    expect(host().querySelectorAll('.banner').length).toBe(1)
    expect(banner()!.className).toBe('banner bad')
  })

  it('draws one notice per draw, never two stacked', () => {
    wire(true)
    draw()
    draw()
    expect(host().querySelectorAll('.banner').length).toBe(1)
  })

  it('says nothing when the source is not installed yet', () => {
    wire(true, false)
    expect(() => draw()).not.toThrow()
    expect(host().children.length).toBe(0)
  })

  it('does nothing at all when the host is not in the document', () => {
    wire(true)
    document.body.innerHTML = ''
    expect(() => {
      setFault('disk full')
      draw()
    }).not.toThrow()
  })
})
