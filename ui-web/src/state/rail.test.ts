// @vitest-environment happy-dom
/* The rail's own state: the three writes setRail made, and the flag the
 * keyboard shortcut used to read off the grid.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import * as rail from './rail'

/* The two regions the state lands on, as page.html serves them: `.app` carries
   no data-rail at all until the boot applies one, and the collapsed-rail twin
   is hidden while the rail stands. */
function markup(): void {
  document.body.innerHTML = '<div class="app"></div><button id="railShow" hidden></button>'
}

const app = (): HTMLElement => document.querySelector('.app') as HTMLElement
const twin = (): HTMLElement => document.getElementById('railShow') as HTMLElement
const root = (): string | undefined => document.documentElement.dataset.rail

beforeEach(markup)

afterEach(() => {
  rail.set(true)
  delete document.documentElement.dataset.rail
  document.body.innerHTML = ''
})

describe('the rail', () => {
  it('is served standing, before anything has written the grid', () => {
    expect(rail.get()).toBe(true)
    expect(app().dataset.rail).toBe(undefined)
    expect(root()).toBe(undefined)
  })

  it('collapses onto the grid, the root and the twin at once', () => {
    rail.set(false)
    expect(rail.get()).toBe(false)
    expect(app().dataset.rail).toBe('off')
    expect(root()).toBe('off')
    /* The twin is the only way back once the column is gone, so it shows
       exactly while the rail does not. */
    expect(twin().hidden).toBe(false)
  })

  it('stands again, and hides the twin with it', () => {
    rail.set(false)
    rail.set(true)
    expect(rail.get()).toBe(true)
    expect(app().dataset.rail).toBe('on')
    expect(root()).toBe('on')
    expect(twin().hidden).toBe(true)
  })

  /* The boot applies the standing state rather than assuming the markup says
     it (app/boot.ts), which is why a page that has never collapsed still
     carries data-rail="on". */
  it('writes the grid on the way open too', () => {
    rail.set(true)
    expect(app().dataset.rail).toBe('on')
    expect(twin().hidden).toBe(true)
  })

  /* What Cmd+\ does. It used to ask the grid -- `dataset.rail === 'off'` -- and
     the served page answers that with false, so the first press collapses; the
     flag answers the same in both directions. */
  it('flips from the flag the way the shortcut asked the grid', () => {
    rail.set(!rail.get())
    expect(app().dataset.rail).toBe('off')
    rail.set(!rail.get())
    expect(app().dataset.rail).toBe('on')
  })
})

/* The window's width, as `matchMedia` answers it: happy-dom does no layout, so
   the list is handed in -- an EventTarget carrying `matches`, which is all the
   module reads of it. */
describe('the window too narrow to hold the rail beside the content', () => {
  const real = window.matchMedia
  let narrow = false
  let asked: string[] = []
  let query: MediaQueryList & { matches: boolean }
  const change = (matches: boolean): void => {
    query.matches = matches
    const e = new Event('change') as Event & { matches: boolean }
    e.matches = matches
    query.dispatchEvent(e)
  }

  beforeEach(() => {
    asked = []
    window.matchMedia = ((q: string) => {
      asked.push(q)
      query = Object.assign(new EventTarget(), { matches: narrow, media: q }) as MediaQueryList & { matches: boolean }
      return query
    }) as typeof window.matchMedia
  })

  afterEach(() => {
    window.matchMedia = real
    narrow = false
  })

  it('tells the boot to stand the rail only where it fits, by the one query the stylesheet folds on', () => {
    expect(rail.stands()).toBe(true)
    narrow = true
    expect(rail.stands()).toBe(false)
    expect(asked).toEqual([rail.FOLDS, rail.FOLDS])
    expect(rail.FOLDS).toBe('(max-width: 639px)')
  })

  it('takes a standing rail down when the window shrinks under the fold', () => {
    rail.watchNarrow()
    change(true)
    expect(rail.get()).toBe(false)
    expect(app().dataset.rail).toBe('off')
    expect(twin().hidden).toBe(false)
  })

  /* No initial check -- the boot asks `stands()` -- and nothing on the way back
     up: a rail the window took down comes back by the twin, like one the reader
     took down, so widening a window does not put a column back over a page the
     reader has since laid out without it. */
  it('does nothing at registration, to a collapsed rail, or when the window grows back', () => {
    narrow = true
    rail.watchNarrow()
    expect(rail.get()).toBe(true)
    expect(app().dataset.rail).toBe(undefined)
    rail.set(false)
    change(true)
    expect(rail.get()).toBe(false)
    change(false)
    expect(rail.get()).toBe(false)
    expect(twin().hidden).toBe(false)
  })
})
