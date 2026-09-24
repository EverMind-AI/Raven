// @vitest-environment happy-dom
/* The rail as the page root renders it.
 *
 * The shape is pinned by the region golden (src/test/regions.test.ts, which
 * renders page.html's body plus this root) and by both boot goldens, so nothing
 * here re-states it. What is here is what a golden of tags, ids, classes and
 * data-* cannot see: the roles and flags that are not data-*, the elements
 * handed over empty, which row opens what, and that the literals come from the
 * catalogue rather than from a copy in the JSX.
 */
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as extAgents from '../features/extAgents/store'
import * as settings from '../features/settings/store'
import * as lang from '../state/lang'
import { mountPageRoot } from '../test/pageRoot'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* The two openers the rows call, kept real apart from the one verb each: the
   modules around them answer for names this file never touches (the Escape
   chain's closers live beside those openers). */
const opened = { list: [] as string[] }
const note = (name: string) => async () => {
  opened.list.push(name)
}
vi.spyOn(extAgents, 'open').mockImplementation(note('agents'))
vi.spyOn(settings, 'open').mockImplementation(note('settings'))

/* Nothing: the page root renders the grid, the column and the collapse's twin,
   so a case gets all three by mounting it. */
const MARKUP = ''

let unmount = (): void => {}

function render(): void {
  unmount()
  document.body.innerHTML = MARKUP
  unmount = mountPageRoot()
}

const el = (id: string): HTMLElement => document.getElementById(id) as HTMLElement
const rail = (): HTMLElement => document.querySelector('.rail') as HTMLElement
const source = (rel: string): string => readFileSync(`src/${rel}`, 'utf8') as string

beforeEach(() => {
  opened.list.length = 0
})

afterEach(() => {
  unmount()
  unmount = () => {}
  delete document.documentElement.dataset.rail
  document.body.innerHTML = ''
})

describe('the rail', () => {
  it('renders the column with the six children page.html had, in order', () => {
    render()
    expect(Array.from(rail().children).map((child) => child.id || child.className)).toEqual([
      'railtop',
      'rail-nav',
      'findBox',
      'list',
      'rail-foot',
      'railGrip',
    ])
  })

  it('renders every id the chrome, the islands and the writers reach for, once each', () => {
    render()
    for (const id of [
      'railBtn', 'findBtn',
      'newBtn', 'agentsBtn',
      'findBox', 'sfind', 'sclr', 'list',
      'upnote', 'importRow', 'meBtn', 'railGrip',
    ]) {
      expect(document.querySelectorAll(`#${id}`), id).toHaveLength(1)
    }
  })

  /* What the goldens drop: they record tag, id, class and data-*, so a deleted
     role, a deleted flag or a blanked literal all pass them. */
  it('keeps the roles, the flags and the served states that are not data-*', () => {
    render()
    expect(el('railBtn').getAttribute('aria-expanded')).toBe('true')
    expect(el('findBtn').getAttribute('aria-expanded')).toBe('false')
    expect(el('railGrip').getAttribute('role')).toBe('separator')
    expect(el('railGrip').getAttribute('aria-orientation')).toBe('vertical')
    expect((el('sfind') as HTMLInputElement).placeholder).not.toBe('')
    /* Three things the page is served closed, empty or hidden. */
    expect(el('findBox').hidden).toBe(true)
    expect(el('sclr').hidden).toBe(true)
    expect(el('upnote').hidden).toBe(true)
    for (const svg of rail().querySelectorAll('svg')) {
      expect(svg.getAttribute('aria-hidden')).toBe('true')
    }
  })

  /* Shared ground, and values nothing has answered yet: the rail island roots
     itself in #list, so React must not own that child list; and the
     aria-current over the nav buttons is written from outside React
     (features/rail/store.ts), which is why none of them carries one here. */
  it('hands the list over empty', () => {
    render()
    expect(el('list').childNodes).toHaveLength(0)
    for (const id of ['newBtn', 'agentsBtn']) {
      expect(el(id).getAttribute('aria-current'), id).toBe(null)
    }
  })

  it('renders a literal in every element that carried one', () => {
    render()
    for (const sel of ['#newBtn span', '#agentsBtn span', '#upnote .t', '#upnote .rl', '.me .who .n']) {
      expect(document.querySelector(sel)?.textContent, sel).not.toBe('')
    }
  })

  it('collapses from its own toggle, and opens the search row from the other', () => {
    render()
    act(() => {
      el('railBtn').click()
    })
    expect(document.querySelector<HTMLElement>('.app')!.dataset.rail).toBe('off')
    expect(el('railShow').hidden).toBe(false)
    act(() => {
      el('findBtn').click()
    })
    expect(el('findBox').hidden).toBe(false)
    expect(el('findBtn').getAttribute('aria-expanded')).toBe('true')
  })

  /* Every row, not just one: a row wired to the wrong opener, or to none at
     all, is invisible to a test that clicks a single one. */
  it('opens the page each row names', () => {
    render()
    for (const id of ['agentsBtn', 'meBtn']) {
      act(() => {
        el(id).click()
      })
    }
    expect(opened.list).toEqual(['agents', 'settings'])
  })

  /* A React onClick leaves no trace on the element -- the root delegates every
     click -- so the count that matters is over the source. The new-task row's
     action belongs to the session rather than to the chrome that carries it, so
     app/install.ts's installActions() binds it by id. */
  it('leaves the new-task row exactly one handler, in the module that owns the action', () => {
    render()
    /* React leaves an empty onclick on every element it takes a click of (the
       trap that makes clicks fire on iOS), so the row it does NOT is bare --
       and a second handler here would run beside the imperative one rather
       than replace it. */
    expect(el('newBtn').onclick).toBe(null)
    expect(el('agentsBtn').onclick).not.toBe(null)
    const wiring = source('app/install.ts')
    expect([...wiring.matchAll(/\$\('#newBtn'\)!?\.onclick/g)]).toHaveLength(1)
    const tag = /<button[^>]*id="newBtn"[^>]*>/.exec(source('chrome/Rail.tsx'))
    expect(tag?.[0]).not.toMatch(/onClick/)
  })
})

/* Last in the file on purpose: applying a language is module state for
   everything after it. Same claim as the two dialogs': every word here is the
   catalogue's, read at render time, so the rail cannot come back in the
   language before the flip. A re-render alone would not show it: React diffs
   against the props it rendered last, so a value it never changes is a value it
   never writes again, and only a remount asks the component what the text
   is. */
describe('the rail once a language is applied', () => {
  it('renders the applied words when the column is mounted again', () => {
    const KEYED = ['#newBtn span', '#agentsBtn span', '#upnote .t', '#upnote .rl', '.me .who .n']
    const words = (): string[] => KEYED.map((sel) => document.querySelector(sel)?.textContent ?? '')
    const hint = (): string => (el('sfind') as HTMLInputElement).placeholder
    render()
    const served = words()
    const servedHint = hint()
    lang.set('zh')
    const applied = words()
    expect(applied).not.toEqual(served)
    expect(hint()).not.toBe(servedHint)
    const appliedHint = hint()
    /* A remount over fresh markup, which is what the pass over the document
       cannot help with: the column renders its own literals unless it reads the
       catalogue itself. */
    render()
    expect(words()).toEqual(applied)
    expect(hint()).toBe(appliedHint)
  })
})
