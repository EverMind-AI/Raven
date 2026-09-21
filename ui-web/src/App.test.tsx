// @vitest-environment happy-dom
/* The page's own root: what it renders, and where.
 *
 * The shape of each region is pinned by the region goldens
 * (src/test/regions.test.ts) and the body's standing order by the boot goldens
 * and src/test/portals.test.ts, which is why nothing here re-states either.
 * What is here is what those goldens cannot see: that the sixteen regions the
 * root renders land at the body in that order, that the shared grounds are
 * handed over empty, and -- since a golden records only tag, id, class and
 * data-* -- that the roles, the labels and the literals are on the markup, that
 * a language pick writes the keyed attributes no golden can hold, and that a
 * re-render does not undo the flags another store writes.
 */
import { act } from 'react'
import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from './App'
import { t, setTranslator } from './i18n/t'
import * as confirmStore from './state/confirm'
import * as detail from './state/detail'
import * as lang from './state/lang'
import * as page from './state/page'
import { BOOT_BODY_ORDER } from './state/portals'
import { bodySiblings } from './test/domSnapshot'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* Opening a page re-marks the rail, which reads the page registry; nothing
   here is about either. */
setTranslator((key) => key)
vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
vi.spyOn(page, 'navState').mockImplementation(() => ({ pages: [], btnOf: () => undefined }))

/* What page.html still carries: the two pre-JavaScript shells and the one empty
   host another root mounts into. The root renders everything else, appended
   after them, which is what a portal at the body does. */
const MARKUP = '<div id="splash"></div><div id="onb" hidden></div><div id="noJs"></div>'

/* The regions this root renders: the body's standing order minus #onb, which is
   markup, and minus the four layers appended while the page installs. */
const RENDERED = BOOT_BODY_ORDER.slice(1, -4)

/* The key BOOT_BODY_ORDER files an element under: its id, or its classes when it
   has none -- read off a signature line, the way src/test/portals.test.ts reads
   it, so a golden's line and a live element reduce the same way. */
const keyOf = (line: string): string => {
  const [, tag = '', id, classes = ''] = /^([a-z]+)(?:#([\w-]+))?((?:\.[^.[]+)*)/.exec(line) ?? []
  return id ? `${tag}#${id}` : `${tag}${classes}`
}

function render(markup: string = MARKUP): void {
  document.body.innerHTML = markup
  const root = createRoot(document.createElement('div'))
  flushSync(() => root.render(<App />))
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('the page root', () => {
  it('appends its regions after the shells the markup carries, in the boot order', () => {
    render()
    expect(bodySiblings().map(keyOf)).toEqual([
      'div#splash',
      'div#onb',
      'div#noJs',
      ...RENDERED,
    ])
  })

  it('renders the dialog interiors inside their own veils, and nothing in the standing hosts', () => {
    render()
    const child = (id: string): string | undefined =>
      document.getElementById(id)?.firstElementChild?.className
    expect(document.getElementById('detail')!.children).toHaveLength(1)
    expect(child('detail')).toBe('dpanel')
    expect(document.getElementById('setVeil')!.children).toHaveLength(1)
    expect(child('setVeil')).toBe('smodal')
    expect(document.getElementById('veil')!.children).toHaveLength(1)
    expect(child('veil')).toBe('sheet')
    expect(document.getElementById('toasts')!.children).toHaveLength(0)
    expect(document.getElementById('menu')!.children).toHaveLength(0)
    expect(document.getElementById('jobVeil')!.children).toHaveLength(0)
  })

  it('renders every id the chrome and the islands reach for, once each', () => {
    render()
    for (const id of [
      'cfTitle', 'cfBody', 'cfNo', 'cfYes',
      'dTitle', 'dClose', 'dBody',
      'setModal', 'snav', 'snavList', 'setTitle', 'setSub', 'setClose', 'spanels',
      'railShow', 'split', 'jobVeil', 'menu', 'toasts',
      'extAgentsPage', 'extAgentsBody',
      'connectionsBody', 'memoryBody', 'cronBody',
    ]) {
      expect(document.querySelectorAll(`#${id}`), id).toHaveLength(1)
    }
  })

  /* Shared ground: the detail-drawer openers append their own host under
     #dBody, the settings island portals its nav into #snavList and roots its
     panels in #spanels, each module page's body is the root of its own island,
     and the three sections another domain fills are rooted in a box of their
     own beside #spanels. React owning any of those child lists would tear down
     what the other side put there. */
  it('hands the shared grounds over empty', () => {
    render()
    for (const id of [
      'dBody', 'snavList', 'spanels',
      'extAgentsBody', 'connectionsBody', 'memoryBody', 'cronBody',
    ]) {
      expect(document.getElementById(id)!.childNodes, id).toHaveLength(0)
    }
  })

  /* The two things the region goldens drop: they record tag, id, class and
     data-*, so a deleted role, a deleted aria-label or a blanked literal all
     pass them. The exact literals are proven once per step by the boot dump
     (my_docs/temp/20260917_ui_web_c14_*.txt) rather than copied here -- the
     repo's source is English and the markup's Chinese belongs in one place --
     so what is pinned here is that each of them still carries one. */
  it('keeps the dialog roles and the labels that are not data-*', () => {
    render()
    const sheet = document.querySelector('.sheet')!
    expect(sheet.getAttribute('role')).toBe('dialog')
    expect(sheet.getAttribute('aria-modal')).toBe('true')
    expect(sheet.getAttribute('aria-labelledby')).toBe('cfTitle')
    const modal = document.getElementById('setModal')!
    expect(modal.getAttribute('role')).toBe('dialog')
    expect(modal.getAttribute('aria-modal')).toBe('true')
    const drawer = document.getElementById('detail')!
    expect(drawer.getAttribute('role')).toBe('dialog')
    expect(drawer.getAttribute('aria-modal')).toBe('true')
    expect(document.getElementById('menu')!.getAttribute('role')).toBe('menu')
    expect(document.getElementById('toasts')!.getAttribute('aria-live')).toBe('polite')
    expect(document.getElementById('dClose')!.getAttribute('aria-label')).not.toBe(null)
    for (const svg of document.querySelectorAll('.dx svg, .icb svg')) {
      expect(svg.getAttribute('aria-hidden')).toBe('true')
      expect(svg.querySelectorAll('path')).toHaveLength(1)
    }
  })

  it('renders a literal in every element that carried one', () => {
    render()
    for (const id of ['cfTitle', 'cfNo', 'cfYes', 'dTitle', 'setTitle']) {
      expect(document.getElementById(id)!.textContent, id).not.toBe('')
    }
    for (const sel of ['#extAgentsPage h2']) {
      expect(document.querySelector(sel)!.textContent, sel).not.toBe('')
    }
    expect(document.querySelector('.wm')!.textContent).not.toBe('')
  })

  /* The served page carries no aria-label, no title and no data-tip on any of
     these: applyI18n was the only writer of them and it ran only on a pick, so
     a page nobody has picked for has none. */
  it('carries no keyed attribute before a language is applied', () => {
    render()
    expect(document.getElementById('railShow')!.getAttribute('aria-label')).toBe(null)
    expect(document.getElementById('railShow')!.dataset.tip).toBe(undefined)
    expect(document.getElementById('detail')!.getAttribute('aria-label')).toBe(null)
    expect(document.getElementById('setModal')!.getAttribute('aria-label')).toBe(null)
    expect(document.getElementById('setClose')!.dataset.tip).toBe(undefined)
    expect(document.getElementById('extAgentsPage')!.getAttribute('aria-label')).toBe(null)
    expect(document.getElementById('wsGrip')!.getAttribute('title')).toBe(null)
  })
})

/* Last in the file on purpose: applying a language is module state for
   everything after it. What the passes over the document used to write on the
   served markup, the regions render -- so this is the whole of what replaced
   them, one case per kind of attribute. */
describe('the page root once a language is applied', () => {
  it('writes the text, the placeholder, the label, the tooltip and the title', () => {
    render()
    act(() => {
      lang.set('en')
    })
    expect(document.querySelector('#extAgentsPage h2')!.textContent).toBe(t('gui.page.agents'))
    expect(document.getElementById('extAgentsPage')!.getAttribute('aria-label')).toBe(t('gui.page.agents'))
    expect(document.getElementById('railShow')!.dataset.tip).toBe(t('gui.expand_rail'))
    expect(document.getElementById('railShow')!.getAttribute('aria-label')).toBe(t('gui.expand_rail'))
    expect(document.getElementById('wsGrip')!.getAttribute('title')).toBe(t('gui.resize_ws'))
    expect(document.getElementById('detail')!.getAttribute('aria-label')).toBe(t('gui.cap_detail'))
    expect(document.getElementById('setModal')!.getAttribute('aria-label')).toBe(t('gui.page.set'))
    expect((document.getElementById('sfind') as HTMLInputElement).placeholder).toBe(t('gui.search_sessions'))
    expect(document.getElementById('cfNo')!.textContent).toBe(t('gui.cancel'))
  })

  /* The flags another store writes on a region this root renders. A pick
     re-renders every section, and React diffs against the props it
     rendered last rather than against the document -- so a value it never
     changes is a value it never writes again, which is what lets
     state/page.ts stay the one writer of the open flags. */
  it('keeps the open flag the page store wrote through a re-render', () => {
    render()
    act(() => {
      page.show('extAgentsPage')
    })
    expect(document.getElementById('extAgentsPage')!.dataset.open).toBe('true')
    act(() => {
      lang.set('zh')
    })
    expect(document.getElementById('extAgentsPage')!.dataset.open).toBe('true')
    act(() => {
      page.show(null)
    })
    expect(document.getElementById('extAgentsPage')!.dataset.open).toBe('false')
  })
})

/* The drawer's own half of state/detail.ts: what only the component can do,
   which is the title and the two closers. Last on purpose -- opening the shared
   drawer blanks its title for good (the store keeps page state, and no card
   ever puts the served dash back), so these cases have to run after the one
   above that reads the served literal. */
describe('the shared detail drawer', () => {
  it('blanks the served title for a card, and never puts it back', () => {
    render()
    const title = (): string | null => document.getElementById('dTitle')!.textContent
    expect(title()).toBe('—')
    act(() => {
      detail.open('memory')
    })
    /* An empty <b> is what turns the header row into a floating close control
       (`.detail header:has(b:empty)`, src/styles/page.css). */
    expect(title()).toBe('')
    expect(document.getElementById('dTitle')!.childNodes).toHaveLength(0)
    act(() => {
      detail.close()
    })
    expect(title()).toBe('')
  })

  it('closes from the close button', () => {
    render()
    act(() => {
      detail.open('memory')
    })
    expect(document.getElementById('detail')!.dataset.open).toBe('true')
    act(() => {
      document.getElementById('dClose')!.click()
    })
    expect(document.getElementById('detail')!.dataset.open).toBe('false')
  })

  it('closes from the scrim, and not from the panel over it', () => {
    render()
    act(() => {
      detail.open('memory')
    })
    act(() => {
      document.querySelector<HTMLElement>('#detail .dpanel')!.click()
    })
    expect(document.getElementById('detail')!.dataset.open).toBe('true')
    act(() => {
      document.getElementById('detail')!.click()
    })
    expect(document.getElementById('detail')!.dataset.open).toBe('false')
  })
})
