// @vitest-environment happy-dom
/* The composer dock as the page root renders it.
 *
 * The shape is pinned by the region golden (src/test/regions.test.ts, which
 * renders page.html's body plus this root) and by both boot goldens, so nothing
 * here re-states it. What is here is what a golden of tags, ids, classes and
 * data-* cannot see: the roles and flags that are not data-*, the elements
 * handed over empty because another writer owns them, that the literals come
 * from the catalogue rather than from a copy in the JSX, the two contracts the
 * field carries -- an IME's Enter, the palette's Escape -- and where the three
 * popovers off the bar stand.
 */
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as composer from '../features/composer/mount'
import * as store from '../features/composer/store'
import { resetTranslator, setTranslator } from '../i18n/t'
import * as session from '../lib/session'
import * as confirmStore from '../state/confirm'
import * as ctx from '../state/ctxChip'
import * as lang from '../state/lang'
import * as pageStore from '../state/page'
import * as perm from '../state/perm'
import * as plus from '../state/plus'
import { setSources } from '../state/sources'
import * as tier from '../state/tier'
import * as wd from '../state/workdir'
import { mountPageRoot } from '../test/pageRoot';

import type { ComposerSource } from '../features/composer/types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* Nothing: the page root renders the chat column, the band and the three
   elements the composer's install() reaches for beside it. */
const MARKUP = ''

let unmount = (): void => {}
let sent: string[] = []

function render(over: Partial<ComposerSource> = {}): void {
  act(() => { unmount() })
  sent = []
  setTranslator((key) => `t:${key}`)
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation((_t, _b, _l, fn) => fn())
  setSources({
    composer: {
      meter: () => '',
      slash: [],
      slashName: (id) => id,
      slashHelp: (id) => id,
      send: (text) => { sent.push(text) },
      stop: () => {},
      ...over,
    } satisfies ComposerSource,
  })
  document.body.innerHTML = MARKUP
  unmount = mountPageRoot()
}

const el = (id: string): HTMLElement => document.getElementById(id) as HTMLElement
/* The wrapper a chip shares with its popover, which is what hides the pair. */
const anchor = (id: string): HTMLElement => el(id).closest('.chrome-anch') as HTMLElement
const dock = (): HTMLElement => document.querySelector('.dock') as HTMLElement
const ta = (): HTMLTextAreaElement => el('ta') as HTMLTextAreaElement

const key = (init: KeyboardEventInit): void => {
  act(() => {
    ta().dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init }))
  })
}

/* The popovers keep their up-or-down and the chips their paint in a store
   rather than on the node (src/state/perm.ts, src/state/plus.ts,
   src/state/workdir.ts, src/state/tier.ts), so all of it outlives a case's
   markup and has to be put back by hand between them. */
beforeEach(() => {
  composer._resetForTests()
  store._resetForTests()
  ctx._resetForTests()
  perm._resetForTests()
  tier._resetForTests()
  plus._resetForTests()
  wd._resetForTests()
})

afterEach(() => {
  act(() => { unmount() })
  unmount = () => {}
  composer._resetForTests()
  store._resetForTests()
  ctx._resetForTests()
  perm._resetForTests()
  tier._resetForTests()
  plus._resetForTests()
  wd._resetForTests()
  session._resetForTests()
  resetTranslator()
  localStorage.clear()
  document.body.innerHTML = ''
})

describe('the dock', () => {
  /* Two, where page.html had four: the five characters that used to perch on
     the composer were the landing page's decoration, and the landing page is
     the composer on its own now. The running strip is a third child only while
     something is running -- it renders nothing at all otherwise. */
  it('renders the band with the children page.html had, in order', () => {
    render()
    expect(Array.from(dock().children).map((child) => child.id || child.className)).toEqual([
      'sheetRack',
      'dock-in',
    ])
  })

  it('renders every id the chrome, the islands and the writers reach for, once each', () => {
    render()
    for (const id of [
      'sheetRack', 'queued', 'ta',
      'plusBtn', 'wdChip', 'wdName', 'permChip', 'permName', 'envChip', 'envName', 'meter', 'ctxChip',
      'modelChip', 'modelName', 'go',
      'slashPop', 'slashList', 'plusPop', 'wdPop', 'permPop', 'wdTag',
    ]) {
      expect(document.querySelectorAll(`#${id}`), id).toHaveLength(1)
    }
  })

  /* What the goldens drop: they record tag, id, class and data-*, so a deleted
     role, a deleted flag or a blanked literal all pass them. */
  it('keeps the roles, the flags and the served states that are not data-*', () => {
    render()
    expect(el('slashPop').getAttribute('role')).toBe('listbox')
    expect(el('permPop').getAttribute('role')).toBe('dialog')
    expect(el('plusPop').getAttribute('role')).toBe('dialog')
    expect(el('wdPop').getAttribute('role')).toBe('dialog')
    expect(el('wdPop').dataset.view).toBe('menu')
    expect(el('plusBtn').getAttribute('aria-haspopup')).toBe('true')
    expect(el('wdChip').getAttribute('aria-haspopup')).toBe('true')
    expect(el('permChip').getAttribute('aria-haspopup')).toBe('true')
    expect(el('permChip').getAttribute('aria-expanded')).toBe('false')
    expect(el('modelChip').getAttribute('aria-haspopup')).toBe('true')
    expect(ta().getAttribute('rows')).toBe('1')
    expect(ta().placeholder).not.toBe('')
    /* Four things the page is served hidden or disabled: the "+" waits for the
       composer's first repaint to say what it can offer, and the title tag for
       a conversation. The workspace chip is a draft's, and the page is served
       on a draft. */
    expect(el('envChip').hidden).toBe(true)
    expect(anchor('plusBtn').hidden).toBe(true)
    expect(anchor('wdChip').hidden).toBe(false)
    expect(el('wdTag').hidden).toBe(true)
    expect(el('ctxChip').hidden).toBe(true)
    expect((el('go') as HTMLButtonElement).disabled).toBe(true)
  })

  /* The warning class and the order it lands in. It was a classList.toggle, so
     `risk` came after `chip` and the pair reads `class="chip risk"` everywhere a
     golden or a stylesheet records it; <PermChip/> renders the whole attribute
     now, and this is what says the string did not change with the writer. */
  it('paints the permission chip in the warning colour, after its own class', () => {
    render()
    expect(el('permChip').className).toBe('chip')
    perm.setFromConfig('full')
    expect(el('permChip').className).toBe('chip risk')
    expect(el('permChip').getAttribute('aria-label')).toBe('t:gui.perm.title: t:gui.perm.full')
  })

  /* The "+" is the same two rows in a draft and in a conversation: a file or a
     deck template can go with any message. The composer's repaint is what tells
     it which of the two the page can honour. */
  it('offers the same two rows on a draft and in a conversation', () => {
    render({ upload: async () => ({ path: '/u/x', size: 1 }), templates: { list: async () => ({ templates: [], available: false }), pick: async () => ({ path: '/u/t', size: 1 }), pages: async () => ({ pages: [] }) } })
    act(() => { composer.goPaint() })
    expect(anchor('plusBtn').hidden).toBe(false)
    act(() => { el('plusBtn').click() })
    expect(el('plusPop').dataset.open).toBe('true')
    expect(el('plusBtn').getAttribute('aria-expanded')).toBe('true')
    expect([...el('plusPop').querySelectorAll('.prow .nm')].map((n) => n.textContent))
      .toEqual(['t:gui.plus.upload', 't:gui.plus.template'])
    act(() => { el('plusBtn').click() })
    expect(el('plusPop').dataset.open).toBe('false')

    session.setCurrent('s1')
    act(() => { composer.goPaint() })
    expect(anchor('plusBtn').hidden).toBe(false)
    act(() => { el('plusBtn').click() })
    expect([...el('plusPop').querySelectorAll('.prow .nm')].map((n) => n.textContent))
      .toEqual(['t:gui.plus.upload', 't:gui.plus.template'])
  })

  it('hides the "+" where neither row can be honoured, and offers only the row that can', () => {
    render()
    act(() => { composer.goPaint() })
    expect(anchor('plusBtn').hidden).toBe(true)
    expect(el('plusPop').dataset.open).toBe('false')
    render({ templates: { list: async () => ({ templates: [], available: false }), pick: async () => ({ path: '/u/t', size: 1 }), pages: async () => ({ pages: [] }) } })
    act(() => { composer.goPaint() })
    expect(anchor('plusBtn').hidden).toBe(false)
    act(() => { el('plusBtn').click() })
    expect([...el('plusPop').querySelectorAll('.prow .nm')].map((n) => n.textContent)).toEqual(['t:gui.plus.template'])
  })

  /* The chip is a draft's: it names the default, then the folder picked, and
     opens the folder list off itself. A conversation cannot change its folder,
     so the chip goes and the tag beside the title says which folder it is. */
  it('shows the workspace chip on a draft and the title tag in a conversation', () => {
    render()
    act(() => { wd.draw() })
    expect(anchor('wdChip').hidden).toBe(false)
    expect(el('wdName').textContent).toBe('t:gui.wd.none')
    expect(el('wdTag').hidden).toBe(true)
    act(() => { wd.pick('/w/thesis') })
    expect(el('wdName').textContent).toBe('thesis')
    expect(el('wdChip').className).toBe('chip chrome-wd-set')
    act(() => { el('wdChip').click() })
    expect(el('wdPop').dataset.open).toBe('true')
    expect(el('wdPop').closest('.chrome-anch')).toBe(anchor('wdChip'))
    act(() => { el('wdChip').click() })
    expect(el('wdPop').dataset.open).toBe('false')

    setSources({
      rail: {
        snapshot: () => ({ rows: [{ id: 's1', title: 's1', workdir: '/w/thesis', persisted: true }], cur: 's1', busy: false }),
        replace: () => {},
        open: () => {},
      },
    })
    session.setCurrent('s1')
    act(() => { wd.draw() })
    expect(anchor('wdChip').hidden).toBe(true)
    expect(el('wdTag').hidden).toBe(false)
    expect(el('wdTag').textContent).toBe('thesis')
    expect(el('wdTag').title).toBe('/w/thesis')
  })

  /* The context ring is served with neither attribute, which is why the store
     carries them as nullable: a data-tip at boot would move what the region
     goldens record. */
  it('draws the ring only once a window is known', () => {
    render()
    expect(el('ctxChip').dataset.tip).toBe(undefined)
    expect(el('ctxChip').getAttribute('aria-label')).toBe(null)
    expect(el('ctxChip').querySelector('.fg')!.getAttribute('stroke-dashoffset')).toBe(null)
    ctx.set(100_000, 200_000)
    expect(el('ctxChip').hidden).toBe(false)
    expect(el('ctxChip').dataset.tip).toBe('t:gui.ctx.tip')
    expect(el('ctxChip').getAttribute('aria-label')).toBe('t:gui.ctx.tip')
    expect(Number(el('ctxChip').querySelector('.fg')!.getAttribute('stroke-dashoffset'))).toBeCloseTo(47.75 / 2, 5)
  })

  /* Empty as served. The sheet rack, the composer's own three roots and the two
     popover stores are what fill them -- shared ground for the first three,
     since React owning those would tear down what the other side put there, and
     page.css reads `.dock .sheets:has(> *)` off the rack, so an empty rack has
     to have no children at all. The two lists and the tier heading are filled
     from a store instead, which is why each has to render nothing rather than
     an empty string until there is something to say. */
  it('hands the rack, the queue, the palette and the two lists over empty', () => {
    render()
    for (const id of ['sheetRack', 'queued', 'slashList', 'plusPop', 'wdPop', 'permPop', 'meter']) {
      expect(el(id).childNodes, id).toHaveLength(0)
    }
    expect(document.querySelectorAll('#sheetRack > *')).toHaveLength(0)
    expect(el('go').childNodes).toHaveLength(0)
  })

  it('renders a literal in every element that carried one', () => {
    render()
    for (const sel of ['#permName', '#envName', '#modelName', '#slashPop .lab']) {
      expect(document.querySelector(sel)?.textContent, sel).not.toBe('')
    }
  })

  /* One click in the band is another module's: the send button belongs to the
     composer store. A React onClick beside it would not replace it -- it would
     run BESIDE the imperative handler, and both would fire on one press. React
     leaves an empty onclick on every element it takes a click of (the trap that
     makes clicks fire on iOS), so a bare .onclick is what says the element is
     still the other writer's, and that trap is what says the buttons are this
     tree's (src/chrome/PlusMenu.tsx, src/chrome/WorkdirChip.tsx,
     src/chrome/PermChip.tsx, src/chrome/ModelChip.tsx). */
  it('takes the chips, and leaves the send button to the module that owns it', () => {
    render()
    expect(el('go').onclick).toBe(null)
    const OWN = ['plusBtn', 'wdChip', 'permChip', 'modelChip']
    for (const id of OWN) expect(typeof el(id).onclick, id).toBe('function')
    for (const node of dock().querySelectorAll('*')) {
      if (OWN.includes(node.id)) continue
      expect((node as HTMLElement).onclick, node.id || node.className).toBe(null)
    }
    /* And the one the composer does bind, once it has: the id it reaches for is
       the one this renders. */
    composer.install()
    expect(el('go').onclick).not.toBe(null)
  })

  /* The tray is not in the markup: it exists only once something is staged, and
     the composer inserts it before .field. The boot goldens have it at that
     position, so the card's own children have to leave room for it there. */
  it('takes the attachment tray between the queue and the field', () => {
    render()
    composer.install()
    act(() => { composer.drawQueue() })
    const box = el('atts')
    expect(box.hidden).toBe(true)
    expect(Array.from(document.querySelector('.dock-in')!.children).map((c) => c.id || c.className)).toEqual([
      'queued', 'atts', 'field', 'under', 'slashPop',
    ])
  })
})

/* An IME sends its keystrokes as keydown too, so while a composition is open
   Enter belongs to the input method. The field stays uncontrolled with native
   listeners for exactly this: React's onKeyDown would see the same flags, but a
   component owning the value would write over the candidate mid-composition. */
describe('the field while an IME is composing', () => {
  it('leaves an Enter with isComposing to the input method', () => {
    render()
    composer.install()
    ta().value = 'a half-typed line'
    key({ key: 'Enter', isComposing: true })
    expect(sent).toEqual([])
    expect(ta().value).toBe('a half-typed line')
  })

  it('leaves an Enter spelled as keyCode 229 to it as well', () => {
    render()
    composer.install()
    ta().value = 'a half-typed line'
    key({ key: 'Enter', keyCode: 229 })
    expect(sent).toEqual([])
    expect(ta().value).toBe('a half-typed line')
  })

  it('still sends on a plain Enter', () => {
    render()
    composer.install()
    ta().value = 'a finished line'
    key({ key: 'Enter' })
    expect(sent).toEqual(['a finished line'])
    expect(ta().value).toBe('')
  })
})

/* The palette's Escape is stopped at the field, because the document owns
   Escape too and dismissing a menu must not fall through to "interrupt the
   running turn". */
describe('the field while the slash palette is open', () => {
  it('closes the palette on Escape and does not let it reach the document', () => {
    render({ slash: [{ id: 'gui.clear', fn: () => {} }] })
    composer.install()
    let reached = 0
    const sentinel = (): void => { reached += 1 }
    document.addEventListener('keydown', sentinel)
    try {
      ta().value = '/'
      act(() => { ta().dispatchEvent(new Event('input')) })
      expect(store.slashIsOpen()).toBe(true)
      key({ key: 'Escape' })
      expect(store.slashIsOpen()).toBe(false)
      expect(el('slashPop').dataset.open).toBe('false')
      expect(reached).toBe(0)
      /* Not stopped when the palette is shut: that Escape is the document's, and
         the terminal branch of its chain is what halts a running turn. */
      key({ key: 'Escape' })
      expect(reached).toBe(1)
    } finally {
      document.removeEventListener('keydown', sentinel)
    }
  })
})

/* Last in the file on purpose: applying a language is module state for
   everything after it. The claim is that every keyed word here is the
   catalogue's rather than the literal the markup was served with -- the
   stand-in translator answers `t:<key>`, so a served word on screen would be
   visible at once -- and that a pick and a remount both leave it that way. */
describe('the dock once a language is applied', () => {
  it('renders the keyed words from the catalogue, before and after a pick', () => {
    const KEYED = ['#slashPop .lab']
    const words = (): string[] => KEYED.map((sel) => document.querySelector(sel)?.textContent ?? '')
    const hint = (): string => ta().placeholder
    const asked = ['t:gui.session_commands']
    render()
    expect(words()).toEqual(asked)
    expect(hint()).toBe('t:gui.composer_ph')
    act(() => { lang.set('zh') })
    expect(words()).toEqual(asked)
    /* A remount over fresh markup: the band would render its own literals again
       if it did not read the catalogue itself. */
    render()
    expect(words()).toEqual(asked)
    expect(hint()).toBe('t:gui.composer_ph')
  })

  /* The three chips carry no key: each is owned by whoever fills it afterwards,
     so a flip must leave the served word alone here and let that owner replace
     it. Two of the three read their own store rather than the language, so the
     flip does not reach them either -- the boot's list and the language
     repaint's list are what call their draw. */
  it('leaves the three unkeyed chip labels to their writers', () => {
    render()
    const labels = (): string[] => ['#permName', '#envName', '#modelName']
      .map((sel) => document.querySelector(sel)?.textContent ?? '')
    const served = labels()
    act(() => { lang.set('en') })
    expect(labels()).toEqual(served)
    for (const sel of ['#permName', '#envName', '#modelName']) {
      for (const name of document.querySelector(sel)!.getAttributeNames()) {
        expect(name, sel).not.toMatch(/^data-i18n/)
      }
    }
  })
})

/* The three popovers off the bar, and the one thing about them a golden of
 * the band cannot see: where each stands.
 *
 * Each is rendered beside its chip, inside an anchor the two share, and hangs
 * off the chip's top edge with the stylesheet. Nothing measures, nothing moves:
 * the fixed-coordinate popovers before them left the card for the body because
 * the card's entrance animation re-based their coordinates, and that whole
 * dance is gone with the coordinates.
 *
 * Last in the file because one case applies a language, which is module state
 * for everything after it.
 */
describe('the anchored popovers', () => {
  interface Popover {
    readonly open: () => void
    readonly close: () => void
    readonly isOpen: () => boolean
  }
  const POPOVERS: ReadonlyArray<{ name: string, pop: string, chip: string, popover: Popover }> = [
    { name: 'plus', pop: 'plusPop', chip: 'plusBtn', popover: plus },
    { name: 'workdir', pop: 'wdPop', chip: 'wdChip', popover: wd },
    { name: 'perm', pop: 'permPop', chip: 'permChip', popover: perm },
  ]

  /* The "+" opens only once the composer source offers it a row, and the
     workspace chip only on a draft with its paint drawn. */
  function ready(): void {
    render({ upload: async () => ({ path: '/u/x', size: 1 }) })
    act(() => { composer.goPaint(); wd.draw() })
  }

  it('stands beside its chip, in the card, open or closed', () => {
    ready()
    for (const { name, pop, chip, popover } of POPOVERS) {
      expect(el(pop).previousElementSibling, name).toBe(el(chip))
      expect(el(pop).parentElement!.className, name).toBe('chrome-anch')
      popover.open()
      expect(el(pop).dataset.open, name).toBe('true')
      expect(el(pop).closest('.dock-in'), name).not.toBeNull()
      expect(el(pop).getAttribute('style'), name).toBeNull()
      popover.close()
      expect(el(pop).parentElement!.className, name).toBe('chrome-anch')
    }
  })

  /* A press on the chip has to toggle once. React's delegated click and an
     imperative .onclick both firing would toggle twice and leave the popover shut
     -- which is what the chrome's own `$('#permChip').onclick` did until this
     step moved it into the component. */
  it('takes exactly one handler per chip, so one press toggles once', () => {
    ready()
    for (const { name, chip, popover } of POPOVERS) {
      act(() => { el(chip).click() })
      expect(popover.isOpen(), name).toBe(true)
      expect(el(chip).getAttribute('aria-expanded'), name).toBe('true')
      act(() => { el(chip).click() })
      expect(popover.isOpen(), name).toBe(false)
      /* The onclick property is React's empty trap, not a second handler:
         calling it is what tells the two apart. */
      const trap = el(chip).onclick!
      trap.call(el(chip), new PointerEvent('click'))
      expect(popover.isOpen(), name).toBe(false)
    }
  })

  /* The rows are React's, in the container React delegates from: a click on
     one reaches its handler, which is the whole reason the popovers stay put. */
  it('takes a click on a row while it stands', () => {
    ready()
    perm.open()
    act(() => { el('permPop').querySelectorAll<HTMLElement>('.prow')[0]!.click() })
    expect(perm.isOpen()).toBe(false)
    expect(perm.current()).toBe('ask')
    wd.open()
    act(() => { [...el('wdPop').querySelectorAll<HTMLElement>('.prow')].find((r) => r.textContent === 't:gui.wd.none')!.click() })
    expect(wd.isOpen()).toBe(false)
  })

  it('survives a language applied while it stands', () => {
    ready()
    for (const { popover } of POPOVERS) popover.open()
    const pops = POPOVERS.map(({ pop }) => el(pop))
    /* Not 'en': the describe above leaves that applied, and a flip to the
       language in force cannot move a key that is read off it. */
    expect(() => {
      act(() => { lang.set('zh') })
    }).not.toThrow()
    for (const [i, { name, pop }] of POPOVERS.entries()) {
      expect(el(pop), name).toBe(pops[i])
      expect(el(pop).dataset.open, name).toBe('true')
      expect(document.querySelectorAll(`#${pop}`), name).toHaveLength(1)
    }
  })
})
