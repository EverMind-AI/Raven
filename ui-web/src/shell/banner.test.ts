// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'

import { draw, setFault } from './banner'
import { resetTranslator, setTranslator } from '../i18n/t'
import * as nav from '../features/plugins/nav'
import { islands } from '../islands'
import * as confirmStore from '../state/confirm'
import * as pageStore from '../state/page'
import { resetSources, setSources } from '../state/sources'
import { mountPageRoot } from '../test/pageRoot'

import type { BannerSource } from './banner'

interface Wired {
  opened: number
}

/* The notice's one action: the capabilities page on its plugin tab, then that
   island's market card. Counted rather than run -- this file answers for the
   strip, not for the page behind it. */
const wired: Wired = { opened: 0 }
vi.spyOn(pageStore, 'show').mockImplementation(() => {})
vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
vi.spyOn(nav, 'openPlugins').mockResolvedValue(undefined)
vi.spyOn(islands.plugins, 'openMarket').mockImplementation((id) => {
  if (id === 'websearch') wired.opened += 1
})

/* The container page.html carries. #bannerHost itself is the page root's now
   (src/chrome/ChatTop.tsx renders the scroller's three grounds), so the bench
   needs the root standing for the host to exist at all. */
const MARKUP = '<div class="app"><div class="main"><div class="split"><div class="chat"><div class="scroll" id="scroll"></div></div></div></div></div>'

let unmount = (): void => {}

function wire(needsWebsearch = false, withSource = true): Wired {
  const w = wired
  w.opened = 0
  setTranslator((key) => key)
  const source: BannerSource = { websearchNeeds: () => needsWebsearch }
  setSources(withSource ? { banner: source } : {})
  unmount()
  document.body.innerHTML = MARKUP
  unmount = mountPageRoot()
  return w
}

const host = (): HTMLElement => document.getElementById('bannerHost')!
const banner = (): HTMLElement | null => host().querySelector('.banner')

afterEach(() => {
  /* A source, before the reset. `setFault` also draws, and a draw with no fault
     left to show consults the seam -- so clearing the module's state needs a
     source installed even after a case that deliberately ran without one. */
  setSources({ banner: { websearchNeeds: () => false } satisfies BannerSource })
  setFault(null)
  resetTranslator()
  resetSources()
  unmount()
  unmount = () => {}
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

  /* This used to assert the opposite -- that a missing source draws nothing and
     does not throw -- and it was written alongside the catch that made it true.
     Both are gone. The catch could not fire on any page that exists (the seam
     installs `DS.banner` at the top level of a layer concatenated in both
     modes, and every caller of `draw` is inside a function), so the only thing
     it could ever silence was a seam that had genuinely come apart: a rename,
     or a manifest reorder putting the install after a draw. The failure it
     bought was the websearch notice quietly never appearing again. */
  it('throws rather than going quiet when its source is missing', () => {
    wire(true, false)
    expect(() => draw()).toThrow('DS.banner is not installed')
  })

  /* The half that must keep working without a source: a memory fault is this
     module's own state, so it draws before the seam is consulted at all. */
  it('still draws a memory fault with no source installed', () => {
    wire(true, false)
    setFault('disk full')
    expect(banner()!.className).toBe('banner bad')
    expect(banner()!.textContent).toContain('disk full')
  })

  it('does nothing at all when the host is not in the document', () => {
    wire(true)
    document.body.innerHTML = ''
    expect(() => {
      setFault('disk full')
      draw()
    }).not.toThrow()
  })

  /* No host means no decision, which is what the question about the host has
     to come BEFORE: every bench that drives a conversation without the chat
     column calls this (state/session/registry.ts, residency.ts), and none of
     them installs the seam a decision would consult. */
  it('asks nothing of its source when there is no host to draw into', () => {
    wire(true, false)
    document.body.innerHTML = ''
    expect(() => draw()).not.toThrow()
  })
})
