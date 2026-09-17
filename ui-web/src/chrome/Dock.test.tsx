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

// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync, readdirSync } from 'node:fs'

import * as composer from '../features/composer/mount'
import * as store from '../features/composer/store'
import { resetShell, setShell } from '../shell/bridge'
import * as ctx from '../shell/ctxchip'
import * as perm from '../shell/perm'
import * as tier from '../shell/tier'
import * as lang from '../state/lang'
import { _resetForTests as resetLayers, host } from '../state/portals'
import { setSources } from '../state/sources'
import { bodySiblings } from '../test/domSnapshot'
import { mountPageRoot } from '../test/pageRoot'

import type { ComposerSource } from '../features/composer/types'
import type { Shell } from '../shell/bridge'

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

/* The two popovers keep their up-or-down in a store rather than on the node
   (src/shell/perm.ts, src/shell/tier.ts), so it outlives a case's markup and
   has to be put back by hand between them. */
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
  resetShell()
  localStorage.clear()
  document.body.innerHTML = ''
})

describe('the dock', () => {
  it('renders the band with the four children page.html had, in order', () => {
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

  /* Three of the clicks in the band are another module's: the send button and
     the paperclip belong to the composer store, and the model chip to the live
     layer. A React onClick beside one of those would not replace it -- it would
     run BESIDE the imperative handler, and both would fire on one press. React
     leaves an empty onclick on every element it takes a click of (the trap that
     makes clicks fire on iOS), so a bare .onclick is what says the element is
     still the other writer's, and that trap is what says the two chips are this
     tree's (src/chrome/PermChip.tsx, src/chrome/TierChip.tsx). */
  it('takes only the two chips, and leaves each other click to the module that owns it', () => {
    render()
    const IMPERATIVE = ['go', 'attBtn', 'modelChip']
    for (const id of IMPERATIVE) expect(el(id).onclick, id).toBe(null)
    const OWN = ['permChip', 'tierChip']
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

/* The two popovers, and the one thing about them a golden of the band cannot
 * see: where each stands.
 *
 * Both are rendered inside the composer card, because that is where the page
 * was served with them, and both are moved to the body the first time they open
 * -- once, and never back. The move is not a preference: the card's entrance
 * animation makes the card a containing block, which re-bases the panel's
 * `position: fixed` against the card instead of the viewport, so a panel left
 * in the card is placed off the wrong box.
 *
 * Last in the file because one case applies a language, which is module state
 * for everything after it.
 */
describe('the two popovers', () => {
  interface Panel {
    readonly open: () => void
    readonly close: () => void
    readonly isOpen: () => boolean
  }
  const PANELS: ReadonlyArray<{ name: string, pop: string, chip: string, panel: Panel }> = [
    { name: 'perm', pop: 'permPop', chip: 'permChip', panel: perm },
    { name: 'tier', pop: 'tierPop', chip: 'tierChip', panel: tier },
  ]

  it('stands in the composer card until it is opened', () => {
    render()
    for (const { name, pop } of PANELS) {
      expect(el(pop).parentElement!.closest('.dock-in'), name).not.toBe(null)
    }
  })

  it('hangs off the body, last of its children, once it opens', () => {
    render()
    for (const { name, pop, panel } of PANELS) {
      panel.open()
      expect(el(pop).parentElement, name).toBe(document.body)
      expect(document.body.lastElementChild, name).toBe(el(pop))
    }
  })

  /* The move is once, not per open: `close` only takes the panel down. A panel
     put back in the card between opens would be placed off the card again. */
  it('stays under the body when it closes', () => {
    render()
    for (const { name, pop, panel } of PANELS) {
      panel.open()
      panel.close()
      expect(el(pop).parentElement, name).toBe(document.body)
      expect(el(pop).dataset.open, name).toBe('false')
      panel.open()
      expect(el(pop).parentElement, name).toBe(document.body)
    }
  })

  /* `--z-picker` is 46 and both panels set an inline 46, so for these three the
     DOM order at the body IS the whole of the stacking decision -- the panel a
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
      for (const { name, pop } of PANELS) {
        const at = order.findIndex((line) => line.startsWith(`div#${pop}.pop`))
        expect(at, name).toBeGreaterThan(order.indexOf('div'))
      }
    } finally {
      resetLayers()
    }
  })

  /* The tier panel's heading and note are written on open, from the catalogue
     that answered, and neither may carry a key: the built-in ladder is a
     Session Tier and reaches sub-agents, a deployment's own catalogue is a
     Session Mode and does not, so a flip walking the document's keys would
     paint the tier wording back over a mode catalogue's. */
  it('leaves the tier panel with no key for a language flip to find', () => {
    render()
    const pop = el('tierPop')
    for (const node of [pop, ...pop.querySelectorAll('*')]) {
      for (const name of node.getAttributeNames()) {
        expect(name, node.id || node.className).not.toMatch(/^data-i18n/)
      }
    }
  })

  /* A press on the chip has to toggle once. React's delegated click and an
     imperative .onclick both firing would toggle twice and leave the panel shut
     -- which is what the chrome's own `$('#permChip').onclick` did until this
     step moved it into the component. */
  it('takes exactly one handler per chip, so one press toggles once', () => {
    render()
    for (const { name, chip, panel } of PANELS) {
      act(() => { el(chip).click() })
      expect(panel.isOpen(), name).toBe(true)
      act(() => { el(chip).click() })
      expect(panel.isOpen(), name).toBe(false)
      /* The onclick property is React's empty trap, not a second handler:
         calling it is what tells the two apart. */
      const trap = el(chip).onclick!
      trap.call(el(chip), new PointerEvent('click'))
      expect(panel.isOpen(), name).toBe(false)
    }
  })

  /* And nothing in the legacy layer takes their click any more: the chrome
     bound both chips by hand until this step. The case above cannot see such a
     binding -- the page root is not what the chrome installs into, and re-adding
     the deleted line leaves it green (measured) -- so the source is where it has
     to show. The pointerdown that closes the two panels names the same two ids
     and is not a binding on them (legacy/demo/040-state.js), which is why the
     line rather than the file is what this reads. */
  it('is the only place in the tree that takes those two clicks', () => {
    const dir = 'src/legacy'
    const files = (readdirSync(dir, { recursive: true }) as string[]).filter((f) => f.endsWith('.js'))
    expect(files.length).toBeGreaterThan(15)
    for (const file of files) {
      const text = readFileSync(`${dir}/${file}`, 'utf8') as string
      for (const line of text.split('\n')) {
        if (!line.includes('#permChip') && !line.includes('#tierChip')) continue
        expect(line, file).not.toMatch(/onclick|addEventListener/)
      }
    }
  })

  /* A panel that has left the card is a child React no longer holds. Safe only
     because none of the card's children is conditional, so React never
     reconciles that child list -- a re-render renders on into it and leaves it
     where it stands. */
  it('survives a language applied after it left the card', () => {
    render()
    perm.open()
    tier.open()
    const pops = PANELS.map(({ pop }) => el(pop))
    /* Not 'en': the describe above leaves that applied, and a flip to the
       language in force cannot move a key that is read off it. */
    expect(() => {
      act(() => { lang.set('zh') })
    }).not.toThrow()
    for (const [i, { name, pop }] of PANELS.entries()) {
      expect(el(pop).parentElement, name).toBe(document.body)
      expect(el(pop), name).toBe(pops[i])
      expect(document.querySelectorAll(`#${pop}`), name).toHaveLength(1)
    }
    expect(el('slashPop').parentElement!.className).toBe('dock-in')
  })
})
