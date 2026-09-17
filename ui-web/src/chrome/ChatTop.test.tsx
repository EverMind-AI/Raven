// @vitest-environment happy-dom
/* The chat column's chrome above the dock, as the page root renders it.
 *
 * The shape is pinned by the region golden (src/test/regions.test.ts, which
 * renders page.html's body plus this root) and by both boot goldens, so nothing
 * here re-states it. What is here is what a golden of tags, ids, classes and
 * data-* cannot see: the flags that are not data-*, the two grounds handed over
 * empty, which button renames, and -- the point of this step -- that a
 * re-render of these four interiors does not take back what the modules that
 * own those values wrote into them imperatively.
 *
 * The banner's two shapes are pinned where they were before they became a
 * component (src/shell/banner.test.ts, ten cases, unchanged).
 */
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'

import { islands } from '../islands'
import * as lang from '../state/lang'
import { mountPageRoot } from '../test/pageRoot'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* The one verb the header calls, kept as a counter: renaming is the rail
   island's, and this file answers for the button rather than for the rename. */
const renames = { n: 0 }
vi.spyOn(islands.rail, 'rename').mockImplementation(() => {
  renames.n += 1
})

/* Nothing: the page root renders the whole chat column, so a case gets the
   five regions here and the dock beside them by mounting it. */
const MARKUP = ''

let unmount = (): void => {}

function render(): void {
  unmount()
  document.body.innerHTML = MARKUP
  unmount = mountPageRoot()
}

const el = (id: string): HTMLElement => document.getElementById(id) as HTMLElement
const top = (): HTMLElement => document.querySelector('.chat > .top') as HTMLElement
const source = (rel: string): string => readFileSync(`src/${rel}`, 'utf8') as string

/* Always to the language that is not applied, because applying one is module
   state for the rest of the file: a second `set('en')` moves no catalogue
   answer, so a case reached with English already applied would pass for a
   component that draws its text from the catalogue too. */
const flip = (): void => {
  const next = lang.get() === 'en' ? 'zh' : 'en'
  act(() => {
    lang.set(next)
  })
}

beforeEach(() => {
  renames.n = 0
})

afterEach(() => {
  unmount()
  unmount = () => {}
  document.body.innerHTML = ''
})

describe('the chat column chrome', () => {
  it('portals each interior into the container page.html carries', () => {
    render()
    expect(Array.from(top().children).map((child) => child.id || child.className)).toEqual([
      'title',
      'renameBtn',
      'spacer',
      'wsBtn',
    ])
    expect(Array.from(el('scroll').children).map((child) => child.id)).toEqual([
      'bannerHost',
      'flash',
      'stage',
    ])
    expect(Array.from(el('backpill').children).map((child) => child.tagName.toLowerCase())).toEqual(['svg'])
    expect(Array.from(el('brand').children).map((child) => child.className)).toEqual(['mk', 'wl'])
    /* Nothing portals into the seam: it has no interior, which is why it is
       the one region of the five this step leaves entirely to page.html. */
    expect(el('wsGrip').childNodes).toHaveLength(0)
  })

  it('renders every id the chrome, the islands and the writers reach for, once each', () => {
    render()
    for (const id of ['title', 'renameBtn', 'wsBtn', 'wsBdg', 'bannerHost', 'flash', 'stage']) {
      expect(document.querySelectorAll(`#${id}`), id).toHaveLength(1)
    }
  })

  /* What the goldens drop: they record tag, id, class and data-*, so a deleted
     flag, a blanked literal or a slot that stopped being empty all pass them. */
  it('keeps the flags, the literals and the served states that are not data-*', () => {
    render()
    expect(el('wsBtn').getAttribute('aria-expanded')).toBe('false')
    expect(el('wsBdg').hidden).toBe(true)
    expect(el('wsBdg').textContent).toBe('2')
    expect(el('title').textContent).not.toBe('')
    expect(document.querySelector('.chat > .top > .spacer')).toBeTruthy()
    expect(document.querySelector('#brand .wl')?.textContent).toBe('Raven Agent')
    /* #brand .mk:empty hides the slot, so the emptiness is load-bearing. */
    expect(document.querySelector('#brand .mk')?.childNodes).toHaveLength(0)
    for (const svg of [...top().querySelectorAll('svg'), ...el('backpill').querySelectorAll('svg')]) {
      expect(svg.getAttribute('aria-hidden')).toBe('true')
    }
  })

  /* Shared ground: the transcript island appends a lane host into #stage and
     the composer appends the live turn's, so React owning that child list would
     tear both out. #bannerHost is the opposite -- this root fills it -- and it
     starts empty because no notice stands. */
  it('hands the scroller grounds over empty', () => {
    render()
    for (const id of ['bannerHost', 'flash', 'stage']) {
      expect(el(id).childNodes, id).toHaveLength(0)
    }
  })

  it('renames from the header button, and leaves it exactly one handler', () => {
    render()
    act(() => {
      el('renameBtn').click()
    })
    expect(renames.n).toBe(1)
    /* React leaves an empty onclick on every element it takes a click of (the
       trap that makes clicks fire on iOS), so a second, imperative handler
       would run beside this one rather than replace it. */
    expect(el('renameBtn').onclick).not.toBe(null)
  })
})

/* Last in the file on purpose: applying a language is module state for
   everything after it.
 *
 * This is the step's own risk, and it is the mirror image of the rail's. Seven
 * modules write h1#title's text, the workspace panel writes the toggle and the
 * badge, and two islands append their hosts into #stage -- all by id, all
 * imperative, and all onto elements this component renders. React diffs
 * against the props it rendered last rather than against the document, so a
 * value it never changes is a value it never writes again; a literal taken
 * through the catalogue instead WOULD change on a flip and take the writer's
 * value with it, which is what these cases refuse.
 */
describe('the chat column chrome when a language is applied', () => {
  it('keeps the heading the session writers put there', () => {
    render()
    el('title').textContent = 'A conversation about grapes'
    flip()
    expect(el('title').textContent).toBe('A conversation about grapes')
    /* Without the region's subscription the flip would re-render nothing and
       the case above would pass for the wrong reason. */
    expect(source('chrome/ChatTop.tsx')).toMatch(/useSyncExternalStore\(lang\.subscribe, lang\.get\)/)
  })

  /* The rename editor swaps the whole heading out (features/rail/store.ts) and
     puts a fresh one back, so during it the element React committed is not in
     the document at all. */
  it('keeps a heading another module swapped out from under it', () => {
    render()
    const input = document.createElement('input')
    input.className = 'titin'
    el('title').replaceWith(input)
    flip()
    expect(top().firstElementChild).toBe(input)
    expect(document.getElementById('title')).toBeNull()
  })

  /* The toggle's own state and the badge's count, not its wording: the panel
     writes data-tip and aria-label off the same key the markup carries, so the
     document pass is a second writer of those two and takes the phrase back to
     "expand" whatever the panel last said -- which it does today, and which
     this step may not change either way. */
  it('keeps the toggle state and the badge the workspace panel wrote', () => {
    render()
    el('wsBtn').setAttribute('aria-expanded', 'true')
    el('wsBdg').textContent = '+3'
    el('wsBdg').hidden = false
    flip()
    expect(el('wsBtn').getAttribute('aria-expanded')).toBe('true')
    expect(el('wsBdg').textContent).toBe('+3')
    expect(el('wsBdg').hidden).toBe(false)
  })

  /* features/composer/mount.tsx moves its host only when something landed
     after it (`stage.lastElementChild !== host`), so the order is the contract
     and not just the membership. */
  it('keeps the hosts the transcript and the composer appended to #stage, in order', () => {
    render()
    const lane = document.createElement('div')
    lane.dataset.tsl = '1'
    const live = document.createElement('div')
    live.dataset.cvl = '1'
    el('stage').appendChild(lane)
    el('stage').appendChild(live)
    flip()
    expect(Array.from(el('stage').children)).toEqual([lane, live])
    expect(el('stage').lastElementChild).toBe(live)
  })
})
