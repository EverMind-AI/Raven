// @vitest-environment happy-dom
/* The page's own root: what it renders, and where.
 *
 * The shape of each interior is pinned by the region goldens
 * (src/test/regions.test.ts), which is why nothing here re-states it. What is
 * here is what those goldens cannot see: that the root adds nothing at the
 * body, that each interior lands inside the container src/page.html still
 * provides, that the three shared grounds are handed over empty, and -- since a
 * golden records only tag, id, class and data-* -- that the roles, the labels
 * and the literals are still on the markup.
 */
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from './App'
import * as detail from './state/detail'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* The three containers page.html carries, as it carries them, plus a fourth
   region the root has nothing to do with. */
const MARKUP =
  '<div class="app"></div>' +
  '<aside class="detail" id="detail" data-open="false"></aside>' +
  '<div class="veil setveil" id="setVeil" data-open="false"></div>' +
  '<div class="veil" id="veil" data-open="false"></div>' +
  '<div class="toasts" id="toasts"></div>'

function render(markup: string = MARKUP): void {
  document.body.innerHTML = markup
  const root = createRoot(document.createElement('div'))
  flushSync(() => root.render(<App />))
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('the page root', () => {
  it('leaves the body with exactly the regions the markup gave it', () => {
    render()
    expect(Array.from(document.body.children).map((el) => el.id || el.className)).toEqual([
      'app',
      'detail',
      'setVeil',
      'veil',
      'toasts',
    ])
  })

  it('portals one interior into each container, and nothing into the rest', () => {
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
    expect(document.querySelector('.app')!.children).toHaveLength(0)
  })

  it('renders every id the chrome and the islands reach for, once each', () => {
    render()
    for (const id of [
      'cfTitle', 'cfBody', 'cfNo', 'cfYes',
      'dTitle', 'dClose', 'dBody',
      'setModal', 'snav', 'snavList', 'setTitle', 'setSub', 'setClose', 'spanels',
    ]) {
      expect(document.querySelectorAll(`#${id}`), id).toHaveLength(1)
    }
  })

  /* Shared ground: the four detail-drawer openers append their own host under
     #dBody, the settings island portals its nav into #snavList and roots its
     panels in #spanels. React owning any of those child lists would tear down
     what the other side put there. */
  it('hands the three shared grounds over empty', () => {
    render()
    for (const id of ['dBody', 'snavList', 'spanels']) {
      expect(document.getElementById(id)!.childNodes, id).toHaveLength(0)
    }
  })

  /* The five keys state/lang.ts's passes rewrite inside these three regions.
     Attributes rather than t() calls on purpose: the store applies a language
     by walking the document (see App.tsx). */
  it('keeps the language keys on the markup', () => {
    render()
    const key = (id: string, attr: string): string | null =>
      document.getElementById(id)!.getAttribute(attr)
    expect(key('cfNo', 'data-i18n')).toBe('gui.cancel')
    expect(key('dClose', 'data-i18n-aria')).toBe('gui.close')
    expect(key('setClose', 'data-i18n-aria')).toBe('gui.close')
    expect(key('setClose', 'data-i18n-tip')).toBe('gui.close')
    expect(key('setModal', 'data-i18n-aria')).toBe('gui.page.set')
    expect(document.querySelector('.wm')!.getAttribute('data-i18n')).toBe('gui.page.set')
  })

  /* The two things the region goldens drop: they record tag, id, class and
     data-*, so a deleted role, a deleted aria-label or a blanked literal all
     pass them. The exact literals are proven once per step by the boot dump
     (my_docs/temp/20260917_ui_web_c2_regions_html_*.txt) rather than copied
     here -- the repo's source is English and the markup's Chinese belongs in
     one place -- so what is pinned here is that each of them still carries
     one. */
  it('keeps the dialog roles and the labels that are not data-*', () => {
    render()
    const sheet = document.querySelector('.sheet')!
    expect(sheet.getAttribute('role')).toBe('dialog')
    expect(sheet.getAttribute('aria-modal')).toBe('true')
    expect(sheet.getAttribute('aria-labelledby')).toBe('cfTitle')
    const modal = document.getElementById('setModal')!
    expect(modal.getAttribute('role')).toBe('dialog')
    expect(modal.getAttribute('aria-modal')).toBe('true')
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
    expect(document.querySelector('.wm')!.textContent).not.toBe('')
  })

  it('renders nothing when the page has no containers to render into', () => {
    render('<div class="app"></div>')
    expect(document.body.innerHTML).toBe('<div class="app"></div>')
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
