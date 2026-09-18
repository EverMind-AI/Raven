// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { markNew as railMarkNew } from '../features/rail/store'
import { resetTranslator, setTranslator } from '../i18n/t'
import { mountPageRoot } from '../test/pageRoot'
import * as confirmStore from './confirm'
import { MORE_ROWS, _resetForTests, draw, mark, toggle } from './navfly'
import * as pageStore from './page'
import { resetSources, setSources } from './sources'

import type { RailSource } from '../features/rail/types'

/* The three openers are direct imports now, so the pages they open are observed
   by standing in for those modules rather than for a shell verb. */
const opens = vi.hoisted(() => ({ list: [] as string[] }))
/* Partial, not wholesale: the page store and the rail call other verbs of each
   of these modules by name. */
vi.mock('../features/connections/wire', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  open: () => opens.list.push('connectionsPage'),
}))
vi.mock('../features/cron/store', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  open: () => opens.list.push('cronPage'),
}))
vi.mock('../features/memory/store', async (original) => ({
  ...(await original<Record<string, unknown>>()),
  open: () => opens.list.push('memoryPage'),
}))

/* NAV_OF, as far as the flyout is concerned: which button a page lights up.
   The three rows all light up the group's own parent. */
const NAV_OF: Record<string, string> = {
  extAgentsPage: 'agentsBtn',
  connectionsPage: 'moreBtn',
  cronPage: 'moreBtn',
  memoryPage: 'moreBtn',
}

interface Harness {
  opened: string[]
  readonly marks: number
}

/* The rail island's own marker is watched rather than stood in for: the
   interplay cases below are about what it and this module write into the same
   strip, so it has to really run. It registers itself on this module when its
   own module evaluates, so the call a case below triggers passes through no
   binding a spy could replace; what a case counts instead is the one question
   every mark asks -- navState(), which markNew() alone calls, once each. */
function install(): Harness {
  opens.list.length = 0
  const nav = vi.spyOn(pageStore, 'navState').mockImplementation(() => ({ pages: Object.keys(NAV_OF), btnOf: (p) => NAV_OF[p] }))
  nav.mockClear()
  const seen: Harness = { opened: opens.list, get marks() { return nav.mock.calls.length } }
  setTranslator((key) => key)
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation((_t, _b, _l, fn) => fn())
  setSources({ rail: { snapshot: () => ({ rows: [], cur: 'a', busy: false, query: '' }) } as unknown as RailSource })
  return seen
}

const fly = (): HTMLElement => document.getElementById('moreFly')!
const rows = (): HTMLElement[] => [...fly().querySelectorAll<HTMLElement>('.navi')]
const marked = (): Array<string | null> => rows().map((b) => b.getAttribute('aria-current'))
const names = (): Array<string | null> => rows().map((b) => b.querySelector('.nm')?.textContent ?? null)
const current = (id: string): string | null => document.getElementById(id)!.getAttribute('aria-current')

/* Whichever page stands open, out of the three the group holds. */
function openPage(id: string | null): void {
  for (const p of Object.keys(NAV_OF)) {
    const n = document.getElementById(p)
    if (n) n.dataset.open = String(p === id)
  }
  document.querySelector<HTMLElement>('.app')!.dataset.page = id ? 'on' : 'off'
}

/* The page root rather than a fixture of the strip: the rows are rendered by
   <MoreFly/> from this store now, and the fold's own button reads `open` off it
   (src/chrome/Rail.tsx), so a case has to mount the column. The store outlives
   a remount -- drawn rows stay drawn -- so each case starts it over. */
let unmount = (): void => {}

beforeEach(() => {
  _resetForTests()
  document.body.innerHTML = ''
  unmount = mountPageRoot()
})

afterEach(() => {
  unmount()
  unmount = () => {}
  document.body.innerHTML = ''
  resetTranslator()
  resetSources()
})

describe('the nav flyout', () => {
  it('draws one row per module it holds, named from the catalogue', () => {
    install()
    draw()
    expect(rows()).toHaveLength(3)
    expect(names()).toEqual(MORE_ROWS.map((r) => r.nameKey))
  })

  /* The rows are the same shape as the modules above them -- .navi, not a
     class of their own -- which is what makes the group read as one list
     getting longer rather than a second, indented one opening under it. */
  it('gives every row its glyph and nothing else', () => {
    install()
    draw()
    for (const b of rows()) {
      expect(b.className).toBe('navi')
      expect(b.children).toHaveLength(2)
      expect(b.children[0]!.tagName.toLowerCase()).toBe('svg')
      expect(b.children[0]!.getAttribute('aria-hidden')).toBe('true')
      expect(b.children[1]!.className).toBe('nm')
    }
  })

  it('redraws over its own rows rather than stacking a second set', () => {
    install()
    draw()
    draw()
    expect(rows()).toHaveLength(3)
  })

  it('marks the row whose page stands open, from the first draw', () => {
    install()
    openPage('connectionsPage')
    draw()
    expect(marked()).toEqual(['false', 'true', 'false'])
  })

  it('navigates on a pick and asks for the marks to be re-decided', () => {
    const seen = install()
    draw()
    rows()[2]!.click()
    expect(seen.opened).toEqual(['memoryPage'])
    expect(seen.marks).toBe(1)
  })

  /* Every row, not just the one below: a row wired to the wrong opener, or to
     none at all, is invisible to a test that picks a single row. */
  it('each row opens the page it names', () => {
    MORE_ROWS.forEach((row, i) => {
      const seen = install()
      draw()
      rows()[i]!.click()
      expect(seen.opened).toEqual([row.page])
    })
  })

  it('stays open on a pick -- it is navigation, not a menu', () => {
    install()
    toggle(true)
    rows()[0]!.click()
    expect(fly().dataset.open).toBe('true')
  })
})

describe('opening and closing the group', () => {
  it('draws the rows on the way open and says so on the button', () => {
    install()
    toggle()
    expect(fly().dataset.open).toBe('true')
    expect(document.getElementById('moreBtn')!.getAttribute('aria-expanded')).toBe('true')
    expect(rows()).toHaveLength(3)
  })

  it('closes again, keeping the rows it drew', () => {
    install()
    toggle()
    toggle()
    expect(fly().dataset.open).toBe('false')
    expect(document.getElementById('moreBtn')!.getAttribute('aria-expanded')).toBe('false')
    expect(rows()).toHaveLength(3)
  })

  it('takes a state rather than a flip when given one', () => {
    install()
    toggle(false)
    expect(fly().dataset.open).toBe('false')
    expect(rows()).toHaveLength(0)
    toggle(true)
    toggle(true)
    expect(fly().dataset.open).toBe('true')
    expect(rows()).toHaveLength(3)
  })

  it('re-decides the marks either way round', () => {
    const seen = install()
    toggle()
    toggle()
    expect(seen.marks).toBe(2)
  })

  it('is what the group button does, without the click reaching the document', () => {
    install()
    const seen: string[] = []
    document.addEventListener('click', () => seen.push('document'))
    document.getElementById('moreBtn')!.click()
    expect(fly().dataset.open).toBe('true')
    expect(seen).toEqual([])
  })
})

describe('marking the rows', () => {
  it('answers that the group is shut, and leaves the rows alone', () => {
    install()
    openPage('connectionsPage')
    draw()
    openPage('cronPage')
    expect(mark()).toBe(false)
    expect(marked()).toEqual(['false', 'true', 'false'])
  })

  it('re-reads every row while the group stands open', () => {
    install()
    draw()
    fly().dataset.open = 'true'
    openPage('cronPage')
    expect(mark()).toBe(true)
    expect(marked()).toEqual(['true', 'false', 'false'])
    openPage(null)
    mark()
    expect(marked()).toEqual(['false', 'false', 'false'])
  })

  it('has nothing to say about a page that is not in the group', () => {
    install()
    draw()
    fly().dataset.open = 'true'
    openPage('extAgentsPage')
    mark()
    expect(marked()).toEqual(['false', 'false', 'false'])
  })
})

/* The seam with the rail island. Both write marks into the same nav strip --
   the rail owns the buttons, this module owns the rows inside #moreFly -- so
   the rail's markNew() drives this module rather than reaching into the rows,
   and reads back whether the group stood open. */
describe('together with the rail', () => {
  it('lets the parent stand in for its pages while the group is shut', () => {
    install()
    openPage('cronPage')
    draw()
    railMarkNew()
    expect(current('moreBtn')).toBe('true')
  })

  it('takes the mark off the parent once its rows can carry it themselves', () => {
    install()
    toggle(true)
    openPage('cronPage')
    railMarkNew()
    expect(current('moreBtn')).toBe('false')
    expect(marked()).toEqual(['true', 'false', 'false'])
  })

  it('leaves the other nav buttons to the rail', () => {
    install()
    toggle(true)
    openPage('extAgentsPage')
    railMarkNew()
    expect(current('agentsBtn')).toBe('true')
    expect(current('moreBtn')).toBe('false')
    expect(marked()).toEqual(['false', 'false', 'false'])
  })
})
