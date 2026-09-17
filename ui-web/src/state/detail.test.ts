// @vitest-environment happy-dom
/* The shared drawer's contract, as the four islands had it between them before
 * this store existed: one host under #dBody at a time, a blanked title, the
 * data-fill asymmetry, the close order the decorator reduce produced, and a
 * card that outlives the flag by the length of the fade.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/* The container, as src/page.html serves it and App.tsx fills it -- the store
   writes the two flags on the first and keeps one host inside the second. */
const MARKUP =
  '<aside class="detail" id="detail" data-open="false">' +
  '<div class="dpanel"><header><b id="dTitle">—</b></header><div class="body" id="dBody"></div></div>' +
  '</aside>'

type Store = typeof import('./detail')

/* A fresh module per case: the store is page state, so its owner, its fade
   timer and its four hosts all outlive a single test otherwise. */
async function load(): Promise<Store> {
  vi.resetModules()
  document.body.innerHTML = MARKUP
  return await import('./detail')
}

const drawer = (): HTMLElement => document.getElementById('detail')!
const body = (): HTMLElement => document.getElementById('dBody')!
const title = (): string | null => document.getElementById('dTitle')!.textContent

afterEach(() => {
  document.body.innerHTML = ''
})

describe('the shared detail drawer', () => {
  it('gives each owner in turn the one host under #dBody', async () => {
    const detail = await load()
    const seen: Element[] = []
    for (const owner of ['memory', 'plugins', 'skills', 'xa'] as const) {
      detail.open(owner)
      expect(body().children, owner).toHaveLength(1)
      expect(body().firstElementChild, owner).toBe(detail.host(owner))
      expect(drawer().dataset.open, owner).toBe('true')
      seen.push(body().firstElementChild!)
    }
    expect(new Set(seen).size).toBe(4)
  })

  it('re-adopts its own host after another opener wiped the body', async () => {
    const detail = await load()
    detail.open('memory')
    const host = detail.host('memory')
    body().innerHTML = ''
    detail.open('memory')
    expect(body().children).toHaveLength(1)
    expect(body().firstElementChild).toBe(host)
  })

  /* Out of the box tree for two of them: #dBody is a grid and the memory and
     agent cards' sections were its own items before a host existed between
     them. The skill and plugin cards were always one box. */
  it('keeps the host display asymmetry the four openers had', async () => {
    const detail = await load()
    expect(detail.host('memory').style.display).toBe('contents')
    expect(detail.host('xa').style.display).toBe('contents')
    expect(detail.host('skills').style.display).toBe('')
    expect(detail.host('plugins').style.display).toBe('')
  })

  /* An empty <b> is what turns the header row into a floating close control
     (`.detail header:has(b:empty)`), so every card blanks the served literal
     and none of them puts it back. */
  it('blanks the served title on the first open and leaves it blank', async () => {
    const detail = await load()
    expect(title()).toBe('—')
    detail.open('skills', { fill: true })
    expect(detail.get().title).toBe('')
    detail.close()
    expect(detail.get().title).toBe('')
  })

  it('fills the panel for the two cards that fetch their body, and no others', async () => {
    const detail = await load()
    for (const owner of ['plugins', 'skills'] as const) {
      detail.open(owner, { fill: true })
      expect(drawer().dataset.fill, owner).toBe('true')
    }
    for (const owner of ['memory', 'xa'] as const) {
      detail.open(owner)
      expect(drawer().hasAttribute('data-fill'), owner).toBe(false)
    }
  })

  /* The order the two close decorators' effects were visible in -- reduce
     applied them outermost-first, so the last registrar ran first -- with the
     two islands that learned of a close from a MutationObserver following in a
     microtask, and the flag write last of all. */
  it('closes in the order the decorator chain made visible', async () => {
    const detail = await load()
    const calls: string[] = []
    for (const owner of ['xa', 'memory', 'skills', 'plugins'] as const) {
      detail.onClose(owner, () => {
        calls.push(owner)
        calls.push(`flag:${drawer().dataset.open ?? ''}`)
      })
    }
    detail.open('plugins', { fill: true })
    detail.close()
    expect(calls).toEqual([
      'plugins',
      'flag:true',
      'skills',
      'flag:true',
      'memory',
      'flag:true',
      'xa',
      'flag:true',
    ])
    expect(drawer().dataset.open).toBe('false')
  })

  it('tells its subscribers about an open and a close', async () => {
    const detail = await load()
    const seen: Array<string | null> = []
    const off = detail.subscribe(() => seen.push(detail.get().owner))
    detail.open('memory')
    detail.close()
    off()
    detail.open('xa')
    expect(seen).toEqual(['memory', 'memory'])
  })

  describe('the fade', () => {
    beforeEach(() => {
      vi.useFakeTimers()
    })
    afterEach(() => {
      vi.useRealTimers()
    })

    it('keeps the card for the length of the fade, then drops it', async () => {
      const detail = await load()
      detail.open('skills', { fill: true })
      detail.close()
      vi.advanceTimersByTime(259)
      expect(detail.get().owner).toBe('skills')
      expect(detail.get().fill).toBe(true)
      expect(drawer().dataset.fill).toBe('true')
      vi.advanceTimersByTime(1)
      expect(detail.get().owner).toBe(null)
      expect(detail.get().fill).toBe(false)
      expect(drawer().hasAttribute('data-fill')).toBe(false)
    })

    it('keeps what a reader opened inside that window', async () => {
      const detail = await load()
      detail.open('skills', { fill: true })
      detail.close()
      vi.advanceTimersByTime(100)
      detail.open('memory')
      vi.advanceTimersByTime(300)
      expect(detail.get().owner).toBe('memory')
      expect(drawer().dataset.open).toBe('true')
    })

    /* What `gen` is for, and why it counts opens rather than cards: reopening
       the drawer inside the fade is the case a pending drop cannot tell from
       its own card still being there. */
    it('counts an open of the same owner after a close, but not a second card', async () => {
      const detail = await load()
      detail.open('memory')
      const first = detail.get().gen
      detail.open('memory')
      expect(detail.get().gen).toBe(first)
      detail.close()
      detail.open('memory')
      expect(detail.get().gen).toBe(first + 1)
      vi.advanceTimersByTime(300)
      expect(detail.get().owner).toBe('memory')
    })
  })

  it('writes nothing when the page has no drawer to write to', async () => {
    vi.resetModules()
    document.body.innerHTML = ''
    const detail = (await import('./detail')) as Store
    expect(() => {
      detail.open('memory')
      detail.close()
    }).not.toThrow()
    expect(document.body.innerHTML).toBe('')
  })
})
