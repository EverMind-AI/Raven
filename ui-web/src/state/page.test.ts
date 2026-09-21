// @vitest-environment happy-dom
/* The open-page store, against what showPageBase did before it.
 *
 * Every effect below was one line of the chrome's own `showPage`, and the two
 * tabs that used to wrap that function are subscribers now -- so the order is
 * part of the contract: the flags, the scroll reset, the app mark, the rail
 * mark and the drawer close all happen first, and only then does each
 * subscriber run, in the order it registered.
 *
 * The sections are src/App.tsx's markup now, and it renders each with the flag
 * the page is served with; this store is still the one that writes it, and the
 * markup below is what each case drives it against.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/** The module pages, as NAV_OF keys them. */
const PAGES = ['extAgentsPage'] as const

interface Fresh {
  page: typeof import('./page')
  /* What each registered slot was asked, in the order the switch asked it. */
  spent: string[]
}

/* Fresh module state per case: the store holds which page is open, a set of
   subscribers and the slot the page's wiring fills, and all three would leak
   into the next case.
   The slot is filled here the way src/app/install.ts fills it, which is also
   what says an unfilled slot is a switch that asks nothing. */
async function fresh(): Promise<Fresh> {
  vi.resetModules()
  const page = await import('./page')
  const spent: string[] = []
  for (const slot of ['markNav'] as const) {
    page.onShow(slot, () => { spent.push(slot) })
  }
  return { page, spent }
}

/* The page as show() reaches it: the shell mark, the sections with their own
   scroller, and the shared drawer the switch closes. */
beforeEach(() => {
  document.body.innerHTML =
    '<div class="app"></div>' +
    PAGES.map((id) => `<section class="page" id="${id}" data-open="false"><div class="work"></div></section>`).join('') +
    '<aside class="detail" id="detail" data-open="false"></aside>'
})

afterEach(() => {
  vi.restoreAllMocks()
})

const flags = (): Record<string, string | undefined> =>
  Object.fromEntries(PAGES.map((id) => [id, document.getElementById(id)!.dataset.open]))

const onlyOpen = (open: string | null): Record<string, string> =>
  Object.fromEntries(PAGES.map((id) => [id, String(id === open)]))

describe('showing a module page', () => {
  it('writes the open flag on every section', async () => {
    const { page } = await fresh()
    page.show('extAgentsPage')
    expect(flags()).toEqual(onlyOpen('extAgentsPage'))
    expect(page.get()).toBe('extAgentsPage')
  })

  it('clears them all when nothing is open', async () => {
    const { page } = await fresh()
    page.show('extAgentsPage')
    page.show(null)
    expect(flags()).toEqual(onlyOpen(null))
    expect(page.get()).toBeNull()
  })

  it('scrolls the page it opens back to the top', async () => {
    const { page } = await fresh()
    const work = document.querySelector<HTMLElement>('#extAgentsPage .work')!
    work.scrollTop = 240
    page.show('extAgentsPage')
    expect(work.scrollTop).toBe(0)
  })

  it('marks the shell while a page is up, and unmarks it after', async () => {
    const { page } = await fresh()
    const app = document.querySelector<HTMLElement>('.app')!
    page.show('extAgentsPage')
    expect(app.dataset.page).toBe('on')
    page.show(null)
    expect(app.dataset.page).toBe('off')
  })

  it('has the rail re-mark itself on every switch', async () => {
    const { page, spent } = await fresh()
    page.show('extAgentsPage')
    page.show(null)
    expect(spent.filter((name) => name === 'markNav')).toHaveLength(2)
  })

  /* Every surface that raises the shared drawer is the settings dialog's now,
     so a page under it is a page the reader left. */
  it('closes the shared drawer for every page', async () => {
    for (const id of PAGES) {
      const { page } = await fresh()
      const drawer = document.getElementById('detail')!
      drawer.dataset.open = 'true'
      page.show(id)
      expect(drawer.dataset.open, id).toBe('false')
    }
  })

  it('spends the registered slot', async () => {
    const { page, spent } = await fresh()
    document.getElementById('detail')!.dataset.open = 'true'
    page.show('extAgentsPage')
    expect(spent).toEqual(['markNav'])
  })
})

describe('the subscribers', () => {
  it('run in registration order, after every effect', async () => {
    const { page } = await fresh()
    const calls: string[] = []
    const record = (name: string) => () => {
      calls.push(`${name}:${page.get()}:${document.getElementById('extAgentsPage')!.dataset.open}`)
    }
    page.subscribe(record('skills'))
    page.subscribe(record('plugins'))
    page.show('extAgentsPage')
    expect(calls).toEqual(['skills:extAgentsPage:true', 'plugins:extAgentsPage:true'])
  })

  it('stop being called once they unsubscribe', async () => {
    const { page } = await fresh()
    let calls = 0
    const off = page.subscribe(() => { calls += 1 })
    page.show('extAgentsPage')
    off()
    page.show(null)
    expect(calls).toBe(1)
  })
})
