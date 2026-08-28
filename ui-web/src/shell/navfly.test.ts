// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { markNew as railMarkNew } from '../features/rail/store'
import { MORE_ROWS, draw, install as installNav, mark, toggle } from './navfly'

import type { Shell } from './bridge'

/* The three openers are direct imports now, so the pages they open are observed
   by standing in for those modules rather than for a shell verb. */
const opens = vi.hoisted(() => ({ list: [] as string[] }))
vi.mock('../features/connections/store', () => ({ open: () => opens.list.push('connPage') }))
vi.mock('../features/cron/store', () => ({ open: () => opens.list.push('cronPage') }))
vi.mock('../features/xa/store', () => ({ open: () => opens.list.push('xaPage') }))

/* NAV_OF, as far as the flyout is concerned: which button a page lights up.
   The three rows all light up the group's own parent. */
const NAV_OF: Record<string, string> = {
  capsPage: 'skillBtn',
  xaPage: 'moreBtn',
  connPage: 'moreBtn',
  cronPage: 'moreBtn',
  memPage: 'memBtn',
}

interface Harness {
  opened: string[]
  marks: number
}

/* `markNew` here is the legacy markNewCurrent(), which is the rail island's
   marker; the interplay tests below swap in the real one. */
function install(over: Partial<Shell> = {}): Harness {
  opens.list.length = 0
  const seen: Harness = { opened: opens.list, marks: 0 }
  const fake: Shell = {
    T: (key) => key,
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: () => {},
    markNew: () => {
      seen.marks += 1
    },
    navState: () => ({ pages: Object.keys(NAV_OF), btnOf: (p) => NAV_OF[p] }),
    ...over,
  }
  window.RavenShell = fake
  window.DS = { sessions: { snapshot: () => ({ rows: [], cur: 'a', busy: false, query: '' }) } }
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

beforeEach(() => {
  document.body.innerHTML =
    '<div class="app" data-page="off">' +
    '<button id="newBtn"></button><button id="skillBtn"></button><button id="plugBtn"></button>' +
    '<button id="memBtn"></button>' +
    '<div class="moresub" id="moreFly" data-open="false"></div>' +
    '<button class="navi more" id="moreBtn" aria-expanded="false">' +
    '<span class="l-more">更多</span><span class="l-less">收起</span></button>' +
    '<section id="capsPage" data-open="false"></section>' +
    '<section id="xaPage" data-open="false"></section>' +
    '<section id="connPage" data-open="false"></section>' +
    '<section id="cronPage" data-open="false"></section>' +
    '<section id="memPage" data-open="false"></section></div>'
})

afterEach(() => {
  delete window.RavenShell
  delete window.DS
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
    openPage('connPage')
    draw()
    expect(marked()).toEqual(['false', 'true', 'false'])
  })

  it('navigates on a pick and asks for the marks to be re-decided', () => {
    const seen = install()
    draw()
    rows()[2]!.click()
    expect(seen.opened).toEqual(['cronPage'])
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
    installNav()
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
    openPage('connPage')
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
    expect(marked()).toEqual(['false', 'false', 'true'])
    openPage(null)
    mark()
    expect(marked()).toEqual(['false', 'false', 'false'])
  })

  it('has nothing to say about a page that is not in the group', () => {
    install()
    draw()
    fly().dataset.open = 'true'
    openPage('memPage')
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
    expect(marked()).toEqual(['false', 'false', 'true'])
  })

  it('leaves the other nav buttons to the rail', () => {
    install()
    toggle(true)
    openPage('memPage')
    railMarkNew()
    expect(current('memBtn')).toBe('true')
    expect(current('moreBtn')).toBe('false')
    expect(marked()).toEqual(['false', 'false', 'false'])
  })
})
