// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import * as store from './perm'
import { resetShell, setShell } from './bridge'
import { mountPageRoot } from '../test/pageRoot'

/* The stored tier is read when the store is reset, so a case that cares about
   it seeds localStorage and then asks for the reset. This was resetModules plus
   a fresh dynamic import, which a store cannot have: <PermChip/> and <PermPop/>
   render from THIS copy of the module, and a second copy would be a store with
   nothing subscribed to it. */
async function load(stored?: string | null): Promise<typeof store> {
  localStorage.clear()
  if (stored != null) localStorage.setItem('raven.perm', stored)
  store._resetForTests()
  return store
}

function wire(): void {
  setShell({ T: (key) => key, confirmAsk: () => {}, showPage: () => {} })
}

/* The chip and the panel are the page root's now (src/chrome/PermChip.tsx,
   src/chrome/PermPop.tsx), so the fixture is the one band they render into --
   the composer card comes with them, and the card is what the panel has to
   open clear of. */
function markup(): void {
  document.body.innerHTML = '<div class="dock"></div>'
}

let unmount = (): void => {}

beforeEach(() => {
  wire()
  markup()
  unmount = mountPageRoot()
})

afterEach(() => {
  unmount()
  unmount = () => {}
  store._resetForTests()
  resetShell()
  document.body.innerHTML = ''
  localStorage.clear()
})

const chip = (): HTMLElement => document.getElementById('permChip')!
const pop = (): HTMLElement => document.getElementById('permPop')!
const rows = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('#permList .prow')]

describe('the permission chip', () => {
  it('defaults to ask, the product default the gate ships with', async () => {
    const perm = await load()
    expect(perm.current()).toBe('ask')
    perm.draw()
    expect(document.getElementById('permName')!.textContent).toBe('gui.perm.ask')
    expect(chip().classList.contains('risk')).toBe(false)
    expect(chip().getAttribute('aria-label')).toBe('gui.perm.title: gui.perm.ask')
  })

  it('marks the risky tier on the chip itself', async () => {
    const perm = await load('full')
    expect(perm.current()).toBe('full')
    perm.draw()
    /* The risky tier is marked as such on the chip, not just in the panel. */
    expect(chip().classList.contains('risk')).toBe(true)
  })

  it('restores a stored tier, and drops the risk mark with it', async () => {
    const perm = await load('smart')
    expect(perm.current()).toBe('smart')
    perm.draw()
    expect(document.getElementById('permName')!.textContent).toBe('gui.perm.smart')
    expect(chip().classList.contains('risk')).toBe(false)
  })

  it('ignores a stored tier no build offers any more', async () => {
    const perm = await load('godmode')
    expect(perm.current()).toBe('ask')
  })

  it('takes the mode the live layer loaded from config', async () => {
    const perm = await load()
    perm.setFromConfig('smart')
    expect(perm.current()).toBe('smart')
    expect(localStorage.getItem('raven.perm')).toBe('smart')
    perm.setFromConfig('godmode')
    expect(perm.current()).toBe('smart')
  })

  it('puts the tier icon in the chip slot', async () => {
    const perm = await load('ask')
    perm.draw()
    const pic = chip().querySelector('.pico')!
    expect(pic.innerHTML).toContain('M9.6 10.2')
    expect(pic.querySelectorAll('path').length).toBe(3)
  })
})

describe('the permission panel', () => {
  it('lists the three tiers strictest first, as a radio group', async () => {
    const perm = await load()
    perm.open()
    expect(pop().dataset.open).toBe('true')
    expect(chip().getAttribute('aria-expanded')).toBe('true')
    expect(rows().map((r) => r.querySelector('.nm')!.textContent)).toEqual([
      'gui.perm.ask',
      'gui.perm.smart',
      'gui.perm.full',
    ])
    expect(rows().map((r) => r.getAttribute('role'))).toEqual(['radio', 'radio', 'radio'])
    expect(rows().map((r) => r.getAttribute('aria-checked'))).toEqual(['true', 'false', 'false'])
    /* Only the risky tier wears the class, and only the chosen one has a tick. */
    expect(rows().filter((r) => r.classList.contains('risk')).length).toBe(1)
    expect(pop().querySelectorAll('svg.tick').length).toBe(1)
  })

  it('reparents the panel to the body so fixed positioning means the viewport', async () => {
    const perm = await load()
    expect(pop().parentElement!.className).toBe('dock-in')
    perm.open()
    /* The composer card animates, which makes it a containing block and quietly
       re-bases position: fixed against it. */
    expect(pop().parentElement).toBe(document.body)
    expect(pop().style.position).toBe('fixed')
    expect(pop().style.zIndex).toBe('46')
    expect(pop().style.right).toBe('auto')
    expect(pop().style.bottom).toBe('auto')
  })

  it('clamps its own left edge into the viewport', async () => {
    const perm = await load()
    perm.open()
    /* happy-dom measures everything as zero, so the useful assertion is the
       floor: the panel never lands at a negative offset. */
    expect(parseFloat(pop().style.left)).toBeGreaterThanOrEqual(8)
    expect(parseFloat(pop().style.top)).toBeGreaterThanOrEqual(8)
  })

  it('opens clear of the composer card, not clear of the chip on it', async () => {
    /* happy-dom measures everything as zero, so the two boxes this turns on are
       given the rects they have on the running page: the card at 436..562 and
       the chip on its bottom bar at 518. Raised off the chip -- which is what
       this did -- the panel's lower edge landed at 512 and its body covered the
       field, the attachment row and anything staged in them. */
    const perm = await load()
    const card = document.querySelector('.dock-in')!
    const box = (el: Element, top: number, bottom: number, height: number): void => {
      el.getBoundingClientRect = () =>
        ({ top, bottom, left: 40, right: 40, width: 0, height, x: 40, y: top } as DOMRect)
    }
    box(card, 436, 562, 126)
    box(chip(), 518, 539, 21)
    /* The panel's own height, which happy-dom would otherwise report as 0 and
       leave the assertion true for the wrong reason. */
    box(pop(), 0, 0, 240)
    perm.open()
    /* 436 - 240 - 6. Above the card's top edge, so nothing of the composer is
       behind it -- and well above the chip's 518, which is the whole point. */
    expect(parseFloat(pop().style.top)).toBe(190)
  })

  it('picks a tier, stores it, repaints the chip and closes', async () => {
    const perm = await load()
    perm.open()
    rows()[0]!.click()
    expect(perm.current()).toBe('ask')
    expect(localStorage.getItem('raven.perm')).toBe('ask')
    expect(document.getElementById('permName')!.textContent).toBe('gui.perm.ask')
    expect(chip().classList.contains('risk')).toBe(false)
    expect(pop().dataset.open).toBe('false')
    expect(chip().getAttribute('aria-expanded')).toBe('false')
  })

  it('rebuilds the list on each open, so the tick follows the pick', async () => {
    const perm = await load()
    perm.open()
    rows()[1]!.click()
    perm.open()
    expect(rows().map((r) => r.getAttribute('aria-checked'))).toEqual(['false', 'true', 'false'])
    expect(rows()[1]!.querySelector('svg.tick')).toBeTruthy()
    expect(pop().querySelectorAll('svg.tick').length).toBe(1)
  })

  it('toggles, since the panel has no close button of its own', async () => {
    const perm = await load()
    perm.toggle()
    expect(perm.isOpen()).toBe(true)
    perm.toggle()
    expect(perm.isOpen()).toBe(false)
  })

  it('survives a storage that refuses to be written', async () => {
    const perm = await load()
    const real = localStorage.setItem.bind(localStorage)
    localStorage.setItem = () => {
      throw new Error('private mode')
    }
    perm.open()
    expect(() => rows()[0]!.click()).not.toThrow()
    /* The pick still applies for this session; only its persistence is lost. */
    expect(perm.current()).toBe('ask')
    localStorage.setItem = real
  })

  it('does nothing at all when the markup is not in the document', async () => {
    const perm = await load()
    document.body.innerHTML = ''
    expect(() => {
      perm.draw()
      perm.open()
      perm.close()
    }).not.toThrow()
    expect(perm.isOpen()).toBe(false)
  })
})

describe('persisting a pick', () => {
  const rows = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('#permList .prow')]

  it('commits the chip only after the write is acknowledged', async () => {
    const perm = await load()
    perm.setPermPersister(() => Promise.resolve(true))
    perm.open()
    rows()[1]!.click()
    await Promise.resolve()
    await Promise.resolve()
    expect(perm.current()).toBe('smart')
  })

  it('keeps the engine mode on a rejected write', async () => {
    const perm = await load()
    perm.setPermPersister(() => Promise.resolve(false))
    perm.open()
    rows()[2]!.click()
    await Promise.resolve()
    await Promise.resolve()
    expect(perm.current()).toBe('ask')
    expect(localStorage.getItem('raven.perm')).not.toBe('full')
  })
})
