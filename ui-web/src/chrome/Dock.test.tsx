// @vitest-environment happy-dom
/* The composer dock as the page root renders it.
 *
 * The shape is pinned by the region golden (src/test/regions.test.ts, which
 * renders page.html's body plus this root) and by both boot goldens, so nothing
 * here re-states it. What is here is what a golden of tags, ids, classes and
 * data-* cannot see: the roles and flags that are not data-*, the elements
 * handed over empty because another writer owns them, that the literals come
 * from the catalogue rather than from a copy in the JSX, and the three
 * contracts the field carries -- an IME's Enter, the palette's Escape, and a
 * popover that leaves the card's DOM without React noticing.
 */
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as composer from '../features/composer/mount'
import * as store from '../features/composer/store'
import { resetTranslator, setTranslator } from '../i18n/t'
import * as confirmStore from '../state/confirm'
import * as ctx from '../state/ctxChip'
import * as lang from '../state/lang'
import * as pageStore from '../state/page'
import * as perm from '../state/perm'
import { _resetForTests as resetLayers, host } from '../state/portals'
import { setSources } from '../state/sources'
import * as tier from '../state/tier'
import { bodySiblings } from '../test/domSnapshot'
import { mountPageRoot } from '../test/pageRoot';

import type { ComposerSource } from '../features/composer/types'
import type { TierSource } from '../state/tier'

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
const dock = (): HTMLElement => document.querySelector('.dock') as HTMLElement
const ta = (): HTMLTextAreaElement => el('ta') as HTMLTextAreaElement

const key = (init: KeyboardEventInit): void => {
  act(() => {
    ta().dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init }))
  })
}

/* The two popovers keep their up-or-down, the chip's paint and the tier popover's
   two headings in a store rather than on the node (src/state/perm.ts,
   src/state/tier.ts), so all of it outlives a case's markup and has to be put
   back by hand between them. */
beforeEach(() => {
  composer._resetForTests()
  store._resetForTests()
  ctx._resetForTests()
  perm._resetForTests()
  tier._resetForTests()
})

afterEach(() => {
  act(() => { unmount() })
  unmount = () => {}
  composer._resetForTests()
  store._resetForTests()
  ctx._resetForTests()
  perm._resetForTests()
  tier._resetForTests()
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
      'attBtn', 'permChip', 'permName', 'envChip', 'envName', 'meter', 'ctxChip',
      'tierChip', 'tierName', 'modelChip', 'modelName', 'go', 'wdChip', 'wdName',
      'slashPop', 'slashList', 'permPop', 'permList', 'tierPop', 'tierPopLab', 'tierList', 'wdPop',
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
    expect(el('tierPop').getAttribute('role')).toBe('dialog')
    expect(el('wdPop').getAttribute('role')).toBe('dialog')
    expect(el('wdChip').getAttribute('aria-haspopup')).toBe('true')
    expect(el('wdChip').getAttribute('aria-expanded')).toBe('false')
    expect(el('tierPop').getAttribute('aria-labelledby')).toBe('tierPopLab')
    expect(el('tierList').getAttribute('role')).toBe('radiogroup')
    expect(el('permChip').getAttribute('aria-haspopup')).toBe('true')
    expect(el('permChip').getAttribute('aria-expanded')).toBe('false')
    expect(el('tierChip').getAttribute('aria-haspopup')).toBe('true')
    expect(el('tierChip').getAttribute('aria-expanded')).toBe('false')
    expect(ta().getAttribute('rows')).toBe('1')
    expect(ta().placeholder).not.toBe('')
    /* Four things the page is served hidden, empty or disabled. */
    expect(el('envChip').hidden).toBe(true)
    expect(el('tierChip').hidden).toBe(true)
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

  /* The bars are the store's markup rather than the two the page is served
     with: `max` is three of them and the served svg has two (state/tier.ts's
     ICO table, src/chrome/TierChip.tsx's SERVED), so a chip still rendering the
     literal reds here instead of passing by coincidence -- which is what the
     ladder's middle rung would do, its glyph being the served one exactly. */
  it('draws the tier chip from the catalogue that answered', async () => {
    const bars = (): Array<string | null> =>
      [...el('tierChip').querySelectorAll('.pico path')].map((path) => path.getAttribute('d'))
    render()
    const menu = [{ id: 'medium' }, { id: 'high' }, { id: 'max' }]
    setSources({
      tier: {
        read: async () => ({ mode: 'max', availableModes: menu }),
        set: async () => ({ mode: 'max', availableModes: menu }),
      } satisfies TierSource,
    })
    expect(bars()).toEqual(['M6 18.5v-4', 'M12 18.5v-9'])

    await act(async () => { await tier.load() })

    expect(el('tierChip').hidden).toBe(false)
    expect(el('tierName').textContent).toBe('Max')
    expect(bars()).toEqual(['M6 18.5v-4', 'M12 18.5v-9', 'M18 18.5v-14'])
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
    for (const id of ['sheetRack', 'queued', 'slashList', 'permList', 'tierList', 'tierPopLab', 'meter']) {
      expect(el(id).childNodes, id).toHaveLength(0)
    }
    expect(document.querySelectorAll('#sheetRack > *')).toHaveLength(0)
    expect(el('go').childNodes).toHaveLength(0)
  })

  it('renders a literal in every element that carried one', () => {
    render()
    for (const sel of ['#permName', '#envName', '#tierName', '#modelName', '#slashPop .lab', '#permPop .lab', '#permPop .note']) {
      expect(document.querySelector(sel)?.textContent, sel).not.toBe('')
    }
    /* The heading and the note of the tier popover come from the store, chosen on
       open from the catalogue that answered, so they carry no key and no
       literal -- and nothing at all before the first open. */
    expect(el('tierPop').querySelector('.note')!.textContent).toBe('')
    for (const node of el('tierPop').querySelectorAll('*')) {
      for (const name of node.getAttributeNames()) expect(name).not.toMatch(/^data-i18n/)
    }
  })

  /* Three of the clicks in the band are another module's: the send button and
     the paperclip belong to the composer store, and the model chip to the live
     layer. A React onClick beside one of those would not replace it -- it would
     run BESIDE the imperative handler, and both would fire on one press. React
     leaves an empty onclick on every element it takes a click of (the trap that
     makes clicks fire on iOS), so a bare .onclick is what says the element is
     still the other writer's, and that trap is what says the three chips are this
     tree's (src/chrome/PermChip.tsx, src/chrome/TierChip.tsx, src/chrome/WorkdirChip.tsx). */
  it('takes only the three chips, and leaves each other click to the module that owns it', () => {
    render()
    const IMPERATIVE = ['go', 'attBtn', 'modelChip']
    for (const id of IMPERATIVE) expect(el(id).onclick, id).toBe(null)
    const OWN = ['permChip', 'tierChip', 'wdChip']
    for (const id of OWN) expect(typeof el(id).onclick, id).toBe('function')
    for (const node of dock().querySelectorAll('*')) {
      if (OWN.includes(node.id)) continue
      expect((node as HTMLElement).onclick, node.id || node.className).toBe(null)
    }
    /* And the two the composer does bind, once it has: the id it reaches for is
       the one this renders. */
    composer.install()
    expect(el('go').onclick).not.toBe(null)
    expect(el('attBtn').onclick).not.toBe(null)
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
      'queued', 'atts', 'field', 'under', 'slashPop', 'permPop', 'tierPop', 'wdPop',
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

/* Both popover writers move their popover to the body the first time it opens,
   because the card's entrance animation makes the card a containing block and
   re-bases the popover's fixed coordinates. React renders the popover inside the
   card, so the move takes a child out from under the portal -- which is safe
   only because none of the card's children is conditional, and React therefore
   never reconciles that child list. */
describe('a popover reparented out of the card', () => {
  it('stays at the body across a re-render, and the card renders on', () => {
    render()
    const pop = el('permPop')
    expect(pop.parentElement!.className).toBe('dock-in')
    document.body.appendChild(pop)
    expect(() => {
      act(() => { lang.set('zh') })
    }).not.toThrow()
    expect(pop.parentElement).toBe(document.body)
    expect(el('permPop')).toBe(pop)
    expect(document.querySelectorAll('#permPop')).toHaveLength(1)
    expect(el('slashPop').parentElement!.className).toBe('dock-in')
    /* Back before the root comes down: React deletes the children it rendered
       from the container it rendered them into, and a node no longer there is a
       removeChild that throws. Nothing unmounts this root in the page. */
    document.querySelector('.dock-in')!.appendChild(pop)
  })
})

/* Last in the file on purpose: applying a language is module state for
   everything after it. The claim is that every keyed word here is the
   catalogue's rather than the literal the markup was served with -- the
   stand-in translator answers `t:<key>`, so a served word on screen would be
   visible at once -- and that a pick and a remount both leave it that way. */
describe('the dock once a language is applied', () => {
  it('renders the keyed words from the catalogue, before and after a pick', () => {
    const KEYED = ['#slashPop .lab', '#permPop .lab', '#permPop .note']
    const words = (): string[] => KEYED.map((sel) => document.querySelector(sel)?.textContent ?? '')
    const hint = (): string => ta().placeholder
    const asked = ['t:gui.session_commands', 't:gui.perm.title', 't:gui.perm.note']
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

  /* The four chips carry no key: each is owned by whoever fills it afterwards,
     so a flip must leave the served word alone here and let that owner replace
     it. Two of the four read their own store rather than the language, so the
     flip does not reach them either -- the boot's list and the language
     repaint's list are what call their draw. */
  it('leaves the four unkeyed chip labels to their writers', () => {
    render()
    const labels = (): string[] => ['#permName', '#envName', '#tierName', '#modelName']
      .map((sel) => document.querySelector(sel)?.textContent ?? '')
    const served = labels()
    act(() => { lang.set('en') })
    expect(labels()).toEqual(served)
    for (const sel of ['#permName', '#envName', '#tierName', '#modelName']) {
      for (const name of document.querySelector(sel)!.getAttributeNames()) {
        expect(name, sel).not.toMatch(/^data-i18n/)
      }
    }
  })
})

/* The two popovers, and the one thing about them a golden of the band cannot
 * see: where each stands.
 *
 * Both are rendered inside the composer card, because that is where the page
 * was served with them, and both are moved to the body the first time they open
 * -- once, and never back. The move is not a preference: the card's entrance
 * animation makes the card a containing block, which re-bases the popover's
 * `position: fixed` against the card instead of the viewport, so a popover left
 * in the card is placed off the wrong box.
 *
 * Last in the file because one case applies a language, which is module state
 * for everything after it.
 */
describe('the two popovers', () => {
  interface Popover {
    readonly open: () => void
    readonly close: () => void
    readonly isOpen: () => boolean
  }
  const POPOVERS: ReadonlyArray<{ name: string, pop: string, chip: string, popover: Popover }> = [
    { name: 'perm', pop: 'permPop', chip: 'permChip', popover: perm },
    { name: 'tier', pop: 'tierPop', chip: 'tierChip', popover: tier },
  ]

  it('stands in the composer card until it is opened', () => {
    render()
    for (const { name, pop } of POPOVERS) {
      expect(el(pop).parentElement!.closest('.dock-in'), name).not.toBe(null)
    }
  })

  it('hangs off the body, last of its children, once it opens', () => {
    render()
    for (const { name, pop, popover } of POPOVERS) {
      popover.open()
      expect(el(pop).parentElement, name).toBe(document.body)
      expect(document.body.lastElementChild, name).toBe(el(pop))
    }
  })

  /* The move is once, not per open: `close` only takes the popover down. A popover
     put back in the card between opens would be placed off the card again. */
  it('stays under the body when it closes', () => {
    render()
    for (const { name, pop, popover } of POPOVERS) {
      popover.open()
      popover.close()
      expect(el(pop).parentElement, name).toBe(document.body)
      expect(el(pop).dataset.open, name).toBe('false')
      popover.open()
      expect(el(pop).parentElement, name).toBe(document.body)
    }
  })

  /* `--z-picker` is 46 and both popovers set an inline 46, so for these three the
     DOM order at the body IS the whole of the stacking decision -- the popover a
     reader just opened has to be the one on top. src/state/portals.ts is where
     that order is declared. */
  it('lands after the model picker, which is what breaks the tie at 46', () => {
    render()
    resetLayers()
    try {
      const picker = host('picker')
      perm.open()
      tier.open()
      const order = bodySiblings()
      /* The picker's wrapper carries neither id nor class, which is why its
         signature is a bare tag. */
      expect(Array.from(document.body.children).indexOf(picker)).toBe(order.indexOf('div'))
      for (const { name, pop } of POPOVERS) {
        const at = order.findIndex((line) => line.startsWith(`div#${pop}.pop`))
        expect(at, name).toBeGreaterThan(order.indexOf('div'))
      }
    } finally {
      resetLayers()
    }
  })

  /* The tier popover's heading and note are the store's, chosen on open from the
     catalogue that answered, and neither may carry a key: the built-in ladder
     is a Session Tier and reaches sub-agents, a deployment's own catalogue is a
     Session Mode and does not, so a flip walking the document's keys would
     paint the tier wording back over a mode catalogue's. */
  it('leaves the tier popover with no key for a language flip to find', () => {
    render()
    const pop = el('tierPop')
    for (const node of [pop, ...pop.querySelectorAll('*')]) {
      for (const name of node.getAttributeNames()) {
        expect(name, node.id || node.className).not.toMatch(/^data-i18n/)
      }
    }
  })

  /* A press on the chip has to toggle once. React's delegated click and an
     imperative .onclick both firing would toggle twice and leave the popover shut
     -- which is what the chrome's own `$('#permChip').onclick` did until this
     step moved it into the component. */
  it('takes exactly one handler per chip, so one press toggles once', () => {
    render()
    for (const { name, chip, popover } of POPOVERS) {
      act(() => { el(chip).click() })
      expect(popover.isOpen(), name).toBe(true)
      act(() => { el(chip).click() })
      expect(popover.isOpen(), name).toBe(false)
      /* The onclick property is React's empty trap, not a second handler:
         calling it is what tells the two apart. */
      const trap = el(chip).onclick!
      trap.call(el(chip), new PointerEvent('click'))
      expect(popover.isOpen(), name).toBe(false)
    }
  })

  /* A popover that has left the card is a child React no longer holds. Safe only
     because none of the card's children is conditional, so React never
     reconciles that child list -- a re-render renders on into it and leaves it
     where it stands. */
  it('survives a language applied after it left the card', () => {
    render()
    perm.open()
    tier.open()
    const pops = POPOVERS.map(({ pop }) => el(pop))
    /* Not 'en': the describe above leaves that applied, and a flip to the
       language in force cannot move a key that is read off it. */
    expect(() => {
      act(() => { lang.set('zh') })
    }).not.toThrow()
    for (const [i, { name, pop }] of POPOVERS.entries()) {
      expect(el(pop).parentElement, name).toBe(document.body)
      expect(el(pop), name).toBe(pops[i])
      expect(document.querySelectorAll(`#${pop}`), name).toHaveLength(1)
    }
    expect(el('slashPop').parentElement!.className).toBe('dock-in')
  })
})
