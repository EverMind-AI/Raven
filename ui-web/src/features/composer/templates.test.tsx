// @vitest-environment happy-dom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SheetRack } from '../../chrome/SheetRack'
import { setTranslator } from '../../i18n/t'
import * as attachmentCache from '../../lib/attachmentCache'
import * as plus from '../../state/plus'
import * as sheetRack from '../../state/sheetRack'
import { resetSources, setSources } from '../../state/sources'
import { AttTray } from './ComposerPage'
import * as store from './store'
import { open, REFRESH_MS } from './templates'

import type { ComposerSource, TemplateRow, TemplatesApi } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const notes = vi.hoisted((): Array<[string, string]> => [])
vi.mock('../transcript/mount', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  note: (label: string, detail: string) => { notes.push([label, detail]) },
}))

const WORDS: Record<string, string> = {
  'gui.tpl.title': 'Pick a deck template', 'gui.tpl.close': 'Close', 'gui.tpl.hint': 'Built on the template by the deck engine',
  'gui.tpl.loading': 'Loading the templates', 'gui.tpl.none': 'No deck templates are installed here', 'gui.tpl.unavailable': 'No deck engine',
  'gui.tpl.fail': '{name} could not be attached', 'gui.att.uploading': 'uploading', 'gui.att.remove': 'remove {name}',
  'gui.tpl.back': 'All templates', 'gui.tpl.use': 'Use this template', 'gui.tpl.pages_loading': 'Loading the pages',
  'gui.tpl.pages_none': 'Cannot render the pages', 'gui.tpl.page': '{n} / {total}', 'gui.tpl.prev': 'Previous page', 'gui.tpl.next': 'Next page',
}

const PAGES = ['data:image/jpeg;base64,AQ==', 'data:image/jpeg;base64,Ag==', 'data:image/jpeg;base64,Aw==']

const ROWS: TemplateRow[] = [
  { name: 'amber_wave_quarterly_summary', label: 'Amber Wave Quarterly Summary', size: 700, cover: 'data:image/jpeg;base64,AA==' },
  { name: 'mint_memphis_thesis_defense', label: 'Mint Memphis Thesis Defense', size: 378, cover: null },
]

/* The dock as the page carries it, plus the rack the sheet docks into and the
   tray the pick lands in. */
const DOCK = `
  <div class="dock">
    <div class="sheets" id="sheetRack"></div>
    <div class="dock-in">
      <div class="atts" id="atts" hidden></div>
      <div class="field"><textarea id="ta" rows="1"></textarea></div>
      <div class="under"><button class="tool-btn" id="attBtn"></button><button class="tool-btn" id="tplBtn" hidden></button>
        <span class="meter" id="meter"></span><button class="go" id="go" disabled></button></div>
    </div>
  </div>`

function wire(templates?: Partial<TemplatesApi>): void {
  setTranslator((key, vars) => {
    const raw = WORDS[key] ?? key
    return vars ? raw.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : raw
  })
  const source: ComposerSource = {
    meter: () => '', slash: [], slashName: (id) => id, slashHelp: (id) => id,
    send: () => {}, stop: () => {},
  }
  if (templates) {
    source.templates = {
      list: async () => ({ templates: ROWS, available: true }),
      pick: async (name) => ({ path: `uploads/${name}.pptx`, size: 1234 }),
      pages: async () => ({ pages: PAGES }),
      ...templates,
    }
  }
  setSources({ composer: source })
  document.body.innerHTML = DOCK
  render(<SheetRack />, { container: document.getElementById('sheetRack')! })
  render(<AttTray />, { container: document.getElementById('atts')! })
}

const settle = async (): Promise<void> => {
  for (let i = 0; i < 4; i += 1) await new Promise((r) => setTimeout(r, 0))
}

afterEach(() => {
  cleanup()
  store._resetForTests()
  attachmentCache._resetForTests()
  sheetRack._resetForTests()
  notes.length = 0
  vi.restoreAllMocks()
  resetSources()
})

describe('the deck template picker', () => {
  it('is offered only where the source lists templates, and the button follows the dock repaint', () => {
    wire()
    expect(store.canPickTemplate()).toBe(false)
    open()
    expect(document.querySelector('.cp-tpl-sheet')).toBeNull()
    store.goPaint()
    expect(plus.get().template).toBe(false)
    wire({})
    expect(store.canPickTemplate()).toBe(true)
    /* The dock is wired before the source is installed, so the "+" menu cannot
       decide at install time; the boot's repaint is when it learns. */
    store.goPaint()
    expect(plus.get().template).toBe(true)
  })

  it('docks a sheet that says it is loading, then shows every template as a card', async () => {
    let release: (r: { templates: TemplateRow[]; available: boolean }) => void = () => {}
    wire({ list: () => new Promise((r) => { release = r }) })
    await act(async () => { open() })
    const sheet = document.querySelector('.csheet.cp-tpl-sheet') as HTMLElement
    expect(sheet.getAttribute('role')).toBe('dialog')
    expect(sheet.querySelector('.q')!.textContent).toBe('Pick a deck template')
    expect(sheet.querySelector('.cp-tpl-empty')!.textContent).toBe('Loading the templates')
    await act(async () => { release({ templates: ROWS, available: true }); await settle() })
    const cards = Array.from(sheet.querySelectorAll('.cp-tpl-card'))
    expect(cards.map((c) => c.getAttribute('aria-label'))).toEqual(['Amber Wave Quarterly Summary', 'Mint Memphis Thesis Defense'])
    /* A cover where the host drew one, the name on a blank card where it did
       not -- and no caption under either: the cover is the card. */
    expect(cards[0]!.querySelector('img.cp-tpl-cover')!.getAttribute('src')).toBe(ROWS[0]!.cover)
    expect(cards[0]!.textContent).toBe('')
    expect(cards[1]!.querySelector('img.cp-tpl-cover')).toBeNull()
    expect(cards[1]!.querySelector('.cp-tpl-cover.cp-tpl-empty')!.textContent).toBe('Mint Memphis Thesis Defense')
  })

  it('opens a cover into its pages, flips through them, and picks from there', async () => {
    let release: (r: { pages: string[] }) => void = () => {}
    wire({ pages: () => new Promise((r) => { release = r }) })
    await act(async () => { open(); await settle() })
    await act(async () => {
      ;(document.querySelector('.cp-tpl-card') as HTMLButtonElement).click()
      await settle()
    })
    const sheet = document.querySelector('.csheet.cp-tpl-sheet') as HTMLElement
    /* The head names the template now, the grid is gone, the pages are on their way. */
    expect(sheet.querySelector('.q')!.textContent).toBe('Amber Wave Quarterly Summary')
    expect(sheet.querySelector('.cp-tpl-grid')).toBeNull()
    expect(sheet.querySelector('.cp-tpl-pages .cp-tpl-count')!.textContent).toBe('Loading the pages')
    await act(async () => { release({ pages: PAGES }); await settle() })
    const strip = sheet.querySelector('.cp-tpl-pages .cp-tpl-strip') as HTMLElement
    expect(Array.from(strip.querySelectorAll('img')).map((i) => i.getAttribute('src'))).toEqual(PAGES)
    expect(sheet.querySelector('.cp-tpl-pages .cp-tpl-count')!.textContent).toBe('1 / 3')
    /* The arrows move the counter; nothing is picked by flipping. */
    Object.defineProperty(strip, 'clientWidth', { value: 600 })
    strip.scrollTo = ((opts: ScrollToOptions) => { strip.scrollLeft = opts.left as number }) as typeof strip.scrollTo
    await act(async () => { (sheet.querySelector('.cp-tpl-arrow[aria-label="Next page"]') as HTMLButtonElement).click() })
    expect(sheet.querySelector('.cp-tpl-pages .cp-tpl-count')!.textContent).toBe('2 / 3')
    expect(document.getElementById('atts')!.hidden).toBe(true)
    /* Back returns to the grid with nothing picked; use picks the open one. */
    await act(async () => { (sheet.querySelector('.cp-tpl-pages .cp-tpl-back') as HTMLButtonElement).click() })
    expect(sheet.querySelector('.cp-tpl-grid')).not.toBeNull()
    await act(async () => {
      ;(document.querySelector('.cp-tpl-card') as HTMLButtonElement).click()
      await settle()
      ;(sheet.querySelector('.cp-tpl-pages .cp-tpl-use') as HTMLButtonElement).click()
      await settle()
    })
    expect(document.querySelector('.cp-tpl-sheet')).toBeNull()
    expect(store.takeAtts()).toEqual(['uploads/amber_wave_quarterly_summary.pptx'])
  })

  it('says so when the pages cannot be rendered, and still lets the template be used', async () => {
    wire({ pages: async () => ({ pages: [] }) })
    await act(async () => { open(); await settle() })
    await act(async () => { (document.querySelector('.cp-tpl-card') as HTMLButtonElement).click(); await settle() })
    const sheet = document.querySelector('.csheet.cp-tpl-sheet') as HTMLElement
    expect(sheet.querySelector('.cp-tpl-pages .cp-tpl-strip .cp-tpl-empty')!.textContent).toBe('Cannot render the pages')
    expect(sheet.querySelector('.cp-tpl-pages .cp-tpl-count')!.textContent).toBe('0 / 0')
    expect(sheet.querySelector('.cp-tpl-pages .cp-tpl-use')).not.toBeNull()
  })

  /* Open the n-th cover and press use: the two clicks a pick is. */
  const pickCard = async (n: number): Promise<void> => {
    await act(async () => {
      ;(document.querySelectorAll('.cp-tpl-card')[n] as HTMLButtonElement).click()
      await settle()
      ;(document.querySelector('.cp-tpl-sheet .cp-tpl-pages .cp-tpl-use') as HTMLButtonElement).click()
      await settle()
    })
  }

  it('stages the picked template in the tray as an uploading chip, then as the attachment the server answered', async () => {
    let release: (r: { path: string; size: number }) => void = () => {}
    wire({ pick: () => new Promise((r) => { release = r }) })
    await act(async () => { open(); await settle() })
    await pickCard(0)
    /* Picking closes the gallery: the chip is the record of the choice now. */
    expect(document.querySelector('.cp-tpl-sheet')).toBeNull()
    const tray = document.getElementById('atts') as HTMLElement
    expect(tray.hidden).toBe(false)
    const chip = tray.querySelector('.att') as HTMLElement
    expect(chip.classList.contains('up')).toBe(true)
    expect(chip.classList.contains('img')).toBe(true)
    expect(store.attsPending()).toBe(1)
    await act(async () => { release({ path: 'uploads/amber_wave_quarterly_summary.pptx', size: 700_000 }); await settle() })
    expect((tray.querySelector('.att') as HTMLElement).classList.contains('up')).toBe(false)
    expect(store.attsPending()).toBe(0)
    expect(store.takeAtts()).toEqual(['uploads/amber_wave_quarterly_summary.pptx'])
    expect(attachmentCache.get('uploads/amber_wave_quarterly_summary.pptx')).toBe(ROWS[0]!.cover)
  })

  it('drops the chip and notes the failure when the server cannot place the template', async () => {
    wire({ pick: async () => { throw new Error('disk full') } })
    await act(async () => { open(); await settle() })
    await pickCard(1)
    expect(document.getElementById('atts')!.hidden).toBe(true)
    expect(notes).toEqual([['Mint Memphis Thesis Defense could not be attached', 'disk full']])
  })

  it('says so when nothing is installed, and closes on Escape or the cross', async () => {
    wire({ list: async () => ({ templates: [], available: false }) })
    await act(async () => { open(); await settle() })
    const sheet = document.querySelector('.cp-tpl-sheet') as HTMLElement
    expect(sheet!.querySelector('.cp-tpl-empty')!.textContent).toBe('No deck engine')
    await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })
    expect(document.querySelector('.cp-tpl-sheet')).toBeNull()
    await act(async () => { open(); await settle() })
    await act(async () => { (document.querySelector('.cp-tpl-sheet .ic') as HTMLButtonElement).click() })
    expect(document.querySelector('.cp-tpl-sheet')).toBeNull()
  })

  it('asks again while a cover is still being drawn, and stops once they have all landed', async () => {
    vi.useFakeTimers()
    const answers = [
      { templates: [{ ...ROWS[0]!, cover: null }, ROWS[1]!], available: true, pending: true },
      { templates: ROWS, available: true, pending: false },
    ]
    const list = vi.fn(async () => answers[Math.min(list.mock.calls.length - 1, answers.length - 1)]!)
    wire({ list })
    await act(async () => { open(); await Promise.resolve(); await Promise.resolve() })
    expect(list).toHaveBeenCalledTimes(1)
    expect(document.querySelector('.cp-tpl-card img.cp-tpl-cover')).toBeNull()
    await act(async () => { vi.advanceTimersByTime(REFRESH_MS); await Promise.resolve(); await Promise.resolve() })
    expect(list).toHaveBeenCalledTimes(2)
    expect(document.querySelector('.cp-tpl-card img.cp-tpl-cover')!.getAttribute('src')).toBe(ROWS[0]!.cover)
    await act(async () => { vi.advanceTimersByTime(REFRESH_MS * 4) })
    expect(list).toHaveBeenCalledTimes(2)
    vi.useRealTimers()
  })

  it('keeps one gallery per conversation: opening again replaces the docked one', async () => {
    wire({})
    await act(async () => { open(); await settle() })
    await act(async () => { open(); await settle() })
    expect(document.querySelectorAll('.cp-tpl-sheet').length).toBe(1)
  })
})
