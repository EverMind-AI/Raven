// @vitest-environment happy-dom
import { act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator, t } from '../i18n/t'
import * as lang from '../state/lang'
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
  lang._resetForTests()
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

  it('puts the tier icon in the chip slot, the same glyph the row draws', async () => {
    const perm = await load('ask')
    perm.draw()
    const pic = chip().querySelector('.pico')!
    expect(pic.tagName.toLowerCase()).toBe('svg')
    perm.open()
    const row = rows().find((r) => r.querySelector('.nm')!.textContent === 'gui.perm.ask')!
    const shape = (el: Element): (string | null)[] =>
      [...el.querySelectorAll('path')].map((p) => p.getAttribute('d'))
    expect(shape(pic).length).toBeGreaterThan(0)
    expect(shape(pic)).toEqual(shape(row.querySelector('.pico')!))
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
    /* The dialog and the group take their name from the visible heading, so a
       reader who has not picked a language still has one, and the words are
       not repeated by an aria-label. */
    expect(pop().getAttribute('aria-label')).toBeNull()
    expect(pop().getAttribute('aria-labelledby')).toBe('perm-pop-title')
    const group = pop().querySelector('[role="radiogroup"]')!
    expect(group.getAttribute('aria-labelledby')).toBe('perm-pop-title')
    expect(group.querySelectorAll('[role="radio"]').length).toBe(3)
    expect(document.getElementById('perm-pop-title')!.textContent).toBe('gui.perm.title')
    /* Only the risky tier wears the class, and only the chosen one has a tick. */
    expect(rows().filter((r) => r.classList.contains('risk')).length).toBe(1)
    expect(pop().querySelectorAll('svg.tick').length).toBe(1)
    /* Each row carries the sentence the tooltip used to hold, under the name,
       and a glyph of its own; the list is named once above them. */
    expect(rows().map((r) => r.querySelector('.sub')!.textContent)).toEqual([
      'gui.perm.ask_h',
      'gui.perm.smart_h',
      'gui.perm.full_h',
    ])
    expect(rows().map((r) => r.getAttribute('title'))).toEqual([null, null, null])
    const glyphs = rows().map((r) => r.querySelector('svg.pico')!.outerHTML)
    expect(new Set(glyphs).size).toBe(rows().length)
    /* Each row's glyph sits in a plate of its own, so the icon reads as the
       mode's mark rather than as a mark loose in the margin. */
    expect(rows().every((r) => r.querySelector('.chrome-perm-badge > svg.pico'))).toBe(true)
    /* The row names its name and describes itself with the sentence. Without
       the label the sentence is part of the name and is read twice. */
    expect(rows().map((r) => r.getAttribute('aria-labelledby'))).toEqual([
      'perm-nm-ask',
      'perm-nm-smart',
      'perm-nm-full',
    ])
    expect(rows().every((r) => {
      const label = document.getElementById(r.getAttribute('aria-labelledby')!)
      return label?.classList.contains('nm') && label.textContent !== r.querySelector('.sub')!.textContent
    })).toBe(true)
    expect(pop().querySelector('.hd .lab')!.textContent).toBe('gui.perm.title')
    expect(pop().querySelector('.chrome-perm-note')!.textContent).toBe('gui.perm.note')
  })

  it('reads the heading and the note in the language in force', async () => {
    const perm = await load()
    /* The rows are built at open and stay in that language; the heading and the
       note name the control, so they follow the catalogue as it stands. */
    resetTranslator()
    act(() => { lang.set('en') })
    perm.open()
    const heading = pop().querySelector('.hd .lab')!
    const note = pop().querySelector('.chrome-perm-note')!
    const en = [heading.textContent, note.textContent]
    act(() => { lang.set('zh') })
    expect([heading.textContent, note.textContent]).toEqual([t('gui.perm.title'), t('gui.perm.note')])
    expect([heading.textContent, note.textContent]).not.toEqual(en)
    expect(pop().getAttribute('aria-labelledby')).toBe('perm-pop-title')
    expect(document.getElementById('perm-pop-title')!.textContent).toBe(t('gui.perm.title'))
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
