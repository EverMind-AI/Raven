// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import { mountPageRoot } from '../test/pageRoot'
import * as store from './perm'

/* The stored tier is read when the store is reset, so a case that cares about
   it seeds localStorage and then asks for the reset. This was resetModules plus
   a fresh dynamic import, which a store cannot have: <PermChip/> and <PermPopover/>
   render from THIS copy of the module, and a second copy would be a store with
   nothing subscribed to it. */
async function load(stored?: string | null): Promise<typeof store> {
  localStorage.clear()
  if (stored != null) localStorage.setItem('raven.perm', stored)
  store._resetForTests()
  return store
}

function wire(): void {
  setTranslator((key) => key)
}

/* The chip and the popover are the page root's now (src/chrome/PermChip.tsx,
   src/chrome/PermPopover.tsx), so the fixture is the one band they render into --
   the composer card comes with them, and the popover hangs off the chip inside it. */
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
  resetTranslator()
  document.body.innerHTML = ''
  localStorage.clear()
})

const chip = (): HTMLElement => document.getElementById('permChip')!
const pop = (): HTMLElement => document.getElementById('permPop')!
const rows = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('#permPop .prow')]

describe('the permission chip', () => {
  it('defaults to smart, the product default the gate ships with', async () => {
    const perm = await load()
    expect(perm.current()).toBe('smart')
    perm.draw()
    expect(document.getElementById('permName')!.textContent).toBe('gui.perm.smart')
    expect(chip().classList.contains('risk')).toBe(false)
    expect(chip().getAttribute('aria-label')).toBe('gui.perm.title: gui.perm.smart')
  })

  it('marks the risky tier on the chip itself', async () => {
    const perm = await load('full')
    expect(perm.current()).toBe('full')
    perm.draw()
    /* The risky tier is marked as such on the chip, not just in the popover. */
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
    expect(perm.current()).toBe('smart')
  })

  it('takes the mode the live layer loaded from config', async () => {
    const perm = await load()
    perm.setFromConfig('ask')
    expect(perm.current()).toBe('ask')
    expect(localStorage.getItem('raven.perm')).toBe('ask')
    perm.setFromConfig('godmode')
    expect(perm.current()).toBe('ask')
  })

  it('puts the tier icon in the chip slot', async () => {
    const perm = await load('ask')
    perm.draw()
    const pic = chip().querySelector('.pico')!
    expect(pic.innerHTML).toContain('M9.6 10.2')
    expect(pic.querySelectorAll('path').length).toBe(3)
  })
})

describe('the permission popover', () => {
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
    expect(rows().map((r) => r.getAttribute('aria-checked'))).toEqual(['false', 'true', 'false'])
    /* Only the risky tier wears the class, and only the chosen one has a tick. */
    expect(rows().filter((r) => r.classList.contains('risk')).length).toBe(1)
    expect(pop().querySelectorAll('svg.tick').length).toBe(1)
    /* Names only: the sentence behind each is a hover away, and there is no
       heading and no note -- the chip already says what is being chosen. */
    expect(rows().map((r) => r.title)).toEqual(['gui.perm.ask_h', 'gui.perm.smart_h', 'gui.perm.full_h'])
    expect(pop().querySelector('.hd, .note, .sub')).toBeNull()
  })

  it('hangs off the chip inside the card, and measures nothing', async () => {
    const perm = await load()
    /* The popover shares an anchor with the chip: the stylesheet hangs it off the
       chip's top edge, so nothing here places it and nothing moves it out of
       the card the way the fixed-coordinate popovers before it were moved. */
    expect(pop().parentElement!.className).toBe('chrome-anch')
    expect(pop().previousElementSibling).toBe(chip())
    perm.open()
    expect(pop().parentElement!.className).toBe('chrome-anch')
    expect(pop().closest('.dock-in')).not.toBeNull()
    expect(pop().getAttribute('style')).toBeNull()
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

  it('toggles, since the popover has no close button of its own', async () => {
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

  it('reaches for nothing in the document, so it runs with the markup gone', async () => {
    const perm = await load()
    document.body.innerHTML = ''
    expect(() => {
      perm.draw()
      perm.open()
    }).not.toThrow()
    /* The store is the popover's state; the markup only shows it. */
    expect(perm.isOpen()).toBe(true)
    perm.close()
    expect(perm.isOpen()).toBe(false)
  })
})

describe('persisting a pick', () => {
  const rows = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('#permPop .prow')]

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
    expect(perm.current()).toBe('smart')
    expect(localStorage.getItem('raven.perm')).not.toBe('full')
  })
})
