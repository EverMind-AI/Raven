// @vitest-environment happy-dom
/* The open-page store, against what showPageBase did before it.
 *
 * Every effect below was one line of the chrome's own `showPage`, and the two
 * tabs that used to wrap that function are subscribers now -- so the
 * order is part of the contract: the seven flags, the scroll reset, the app
 * mark, the rail mark and the overlay closes all happen first, and only then
 * does each subscriber run, in the order it registered.
 *
 * The seven sections are src/App.tsx's markup now, and it renders each with the
 * flag the page is served with; this store is still the one that writes it, and
 * the markup below is what each case drives it against.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/** The seven module pages, as NAV_OF keys them. */
const PAGES = ['capsPage', 'xaPage', 'connPage', 'memPage', 'pbPage', 'kbPage', 'cronPage'] as const

interface Fresh {
  page: typeof import('./page')
  islands: (typeof import('../features/registry'))['islands']
}

/* Fresh module state per case: the store holds which page is open and a set of
   subscribers, and both would leak into the next case. The island bag is taken
   from the same reset graph the store just got, the way
   scripts/module-harness.mjs does it. */
async function fresh(): Promise<Fresh> {
  vi.resetModules()
  const page = await import('./page')
  const { islands } = await import('../features/registry')
  vi.spyOn(islands.rail, 'markNew').mockImplementation(() => {})
  vi.spyOn(islands.connections, 'closeDialog').mockImplementation(() => {})
  vi.spyOn(islands.cron, 'closeSheet').mockImplementation(() => {})
  return { page, islands }
}

/* The page as show() reaches it: the shell mark, the seven sections with their
   own scroller, and the shared drawer the switch closes. */
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
  it('writes the open flag on all seven sections', async () => {
    const { page } = await fresh()
    page.show('memPage')
    expect(flags()).toEqual(onlyOpen('memPage'))
    expect(page.get()).toBe('memPage')
  })

  it('clears all seven when nothing is open', async () => {
    const { page } = await fresh()
    page.show('memPage')
    page.show(null)
    expect(flags()).toEqual(onlyOpen(null))
    expect(page.get()).toBeNull()
  })

  it('scrolls the page it opens back to the top', async () => {
    const { page } = await fresh()
    const work = document.querySelector<HTMLElement>('#kbPage .work')!
    work.scrollTop = 240
    page.show('kbPage')
    expect(work.scrollTop).toBe(0)
  })

  it('marks the shell while a page is up, and unmarks it after', async () => {
    const { page } = await fresh()
    const app = document.querySelector<HTMLElement>('.app')!
    page.show('pbPage')
    expect(app.dataset.page).toBe('on')
    page.show(null)
    expect(app.dataset.page).toBe('off')
  })

  it('has the rail re-mark itself on every switch', async () => {
    const { page, islands } = await fresh()
    page.show('xaPage')
    page.show(null)
    expect(islands.rail.markNew).toHaveBeenCalledTimes(2)
  })

  /* caps and memory both render into the shared drawer, so opening either of
     them must not close what it is about to fill. */
  it('closes the shared drawer for every page except caps and memory', async () => {
    for (const id of PAGES) {
      const { page } = await fresh()
      const drawer = document.getElementById('detail')!
      drawer.dataset.open = 'true'
      page.show(id)
      expect(drawer.dataset.open, id).toBe(id === 'capsPage' || id === 'memPage' ? 'true' : 'false')
    }
  })

  it('closes a page-owned overlay only when the page it belongs to is not the one opening', async () => {
    const { page, islands } = await fresh()
    page.show('connPage')
    expect(islands.connections.closeDialog).not.toHaveBeenCalled()
    expect(islands.cron.closeSheet).toHaveBeenCalledTimes(1)
    page.show('cronPage')
    expect(islands.connections.closeDialog).toHaveBeenCalledTimes(1)
    expect(islands.cron.closeSheet).toHaveBeenCalledTimes(1)
  })
})

describe('the subscribers', () => {
  it('run in registration order, after every effect', async () => {
    const { page } = await fresh()
    const calls: string[] = []
    const record = (name: string) => () => {
      calls.push(`${name}:${page.get()}:${document.getElementById('memPage')!.dataset.open}`)
    }
    page.subscribe(record('skills'))
    page.subscribe(record('plugins'))
    page.show('memPage')
    expect(calls).toEqual(['skills:memPage:true', 'plugins:memPage:true'])
  })

  it('stop being called once they unsubscribe', async () => {
    const { page } = await fresh()
    let calls = 0
    const off = page.subscribe(() => { calls += 1 })
    page.show('xaPage')
    off()
    page.show(null)
    expect(calls).toBe(1)
  })
})
