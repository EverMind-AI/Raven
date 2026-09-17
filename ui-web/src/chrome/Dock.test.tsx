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
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import * as composer from '../features/composer/mount'
import * as store from '../features/composer/store'
import { resetShell, setShell } from '../shell/bridge'
import * as ctx from '../shell/ctxchip'
import * as lang from '../state/lang'
import { setSources } from '../state/sources'
import { mountPageRoot } from '../test/pageRoot'

import type { ComposerSource } from '../features/composer/types'
import type { Shell } from '../shell/bridge'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* The chat column page.html carries, down to the three elements the composer's
   install() reaches for beside the dock. */
const MARKUP = `
  <div class="chat">
    <div class="scroll" id="scroll"><div class="col" id="stage"></div></div>
    <button class="backpill" id="backpill" hidden></button>
    <div class="dock"></div>
  </div>`

let unmount = (): void => {}
let sent: string[] = []

function render(over: Partial<ComposerSource> = {}): void {
  act(() => { unmount() })
  sent = []
  const shell: Shell = {
    T: (key) => `t:${key}`,
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: () => {},
  }
  setShell(shell)
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

beforeEach(() => {
  composer._resetForTests()
  store._resetForTests()
  ctx._resetForTests()
})

afterEach(() => {
  act(() => { unmount() })
  unmount = () => {}
  composer._resetForTests()
  store._resetForTests()
  ctx._resetForTests()
  resetShell()
  localStorage.clear()
  document.body.innerHTML = ''
})

describe('the dock', () => {
  it('portals the four children into the band, in the order page.html had them', () => {
    render()
    expect(Array.from(dock().children).map((child) => child.id || child.className)).toEqual([
      'crew crew-back',
      'sheetRack',
      'dock-in',
      'crew crew-front',
    ])
  })

  it('renders every id the chrome, the islands and the writers reach for, once each', () => {
    render()
    for (const id of [
      'sheetRack', 'queued', 'ta',
      'attBtn', 'permChip', 'permName', 'envChip', 'envName', 'meter', 'ctxChip',
      'tierChip', 'tierName', 'modelChip', 'modelName', 'go',
      'slashPop', 'slashList', 'permPop', 'permList', 'tierPop', 'tierPopLab', 'tierList',
    ]) {
      expect(document.querySelectorAll(`#${id}`), id).toHaveLength(1)
    }
    expect(dock().querySelectorAll('.crew .rv')).toHaveLength(5)
  })

  /* What the goldens drop: they record tag, id, class and data-*, so a deleted
     role, a deleted flag or a blanked literal all pass them. */
  it('keeps the roles, the flags and the served states that are not data-*', () => {
    render()
    expect(el('slashPop').getAttribute('role')).toBe('listbox')
    expect(el('permPop').getAttribute('role')).toBe('dialog')
    expect(el('tierPop').getAttribute('role')).toBe('dialog')
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
    for (const crew of dock().querySelectorAll('.crew')) {
      expect(crew.getAttribute('aria-hidden')).toBe('true')
    }
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

  /* Shared ground: the sheet rack, the composer's own three roots and the two
     popover writers fill these. React owning any of them would tear down what
     the other side put there -- and page.css reads `.dock .sheets:has(> *)` off
     the rack, so an empty rack has to have no children at all. */
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
    /* The heading and the note of the tier panel are written on open, from the
       catalogue that answered, so they carry no key and no literal. */
    expect(el('tierPop').querySelector('.note')!.textContent).toBe('')
    for (const node of el('tierPop').querySelectorAll('*')) {
      for (const name of node.getAttributeNames()) expect(name).not.toMatch(/^data-i18n/)
    }
  })

  /* Not one click is this component's: the send button and the paperclip belong
     to the composer store, the two chips to the popover writers the chrome
     binds, and the model chip to the live layer. A React onClick here would not
     replace any of those -- it would run BESIDE the imperative handler, and
     both would fire on one press. React leaves an empty onclick on every
     element it takes a click of (the trap that makes clicks fire on iOS), so a
     bare .onclick is what says the element is still the other writer's. */
  it('takes no click of its own, and leaves each one to the module that owns it', () => {
    render()
    const IMPERATIVE = ['go', 'attBtn', 'permChip', 'tierChip', 'modelChip']
    for (const id of IMPERATIVE) expect(el(id).onclick, id).toBe(null)
    for (const node of dock().querySelectorAll('*')) {
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
      'queued', 'atts', 'field', 'under', 'slashPop', 'permPop', 'tierPop',
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

/* Both popover writers move their panel to the body the first time it opens,
   because the card's entrance animation makes the card a containing block and
   re-bases the panel's fixed coordinates. React renders the panel inside the
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
   everything after it. Same agreement as the rail's -- the pass state/lang.ts
   makes over the document's data-i18n attributes, and the component rendering
   the same key through lang.text -- so the dock cannot come back in the served
   language once a flip has moved it. */
describe('the dock once a language is applied', () => {
  it('renders the applied words when the band is mounted again', () => {
    const KEYED = ['#slashPop .lab', '#permPop .lab', '#permPop .note']
    const words = (): string[] => KEYED.map((sel) => document.querySelector(sel)?.textContent ?? '')
    const hint = (): string => ta().placeholder
    render()
    const served = words()
    const servedHint = hint()
    act(() => { lang.set('en') })
    const applied = words()
    expect(applied).not.toEqual(served)
    expect(hint()).not.toBe(servedHint)
    const appliedHint = hint()
    /* A remount over fresh markup, which is what the pass over the document
       cannot help with: the band renders its own literals unless it reads the
       catalogue itself. */
    render()
    expect(words()).toEqual(applied)
    expect(hint()).toBe(appliedHint)
  })

  /* The four chips carry no key: each is owned by the writer that fills it
     afterwards, so a flip must leave the served word alone here and let that
     writer replace it. */
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
