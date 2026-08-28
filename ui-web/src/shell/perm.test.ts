// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { Shell } from './bridge'

/* The stored tier is read once at module load, so each case that cares about it
   has to seed localStorage and then import a fresh copy. resetModules plus a
   dynamic import is the whole trick. */
async function load(stored?: string | null): Promise<typeof import('./perm')> {
  localStorage.clear()
  if (stored != null) localStorage.setItem('raven.perm', stored)
  const { resetModules } = await import('vitest').then((v) => ({ resetModules: v.vi.resetModules }))
  resetModules()
  return import('./perm')
}

/* The same three elements page.html carries, in the same nesting and with the
   same tags: the chip holds the icon slot and the label, the panel holds the
   list. `.pico` is the svg itself there, not a span around one. */
function markup(): void {
  document.body.innerHTML = `
    <div class="card">
      <button class="chip" id="permChip" aria-expanded="false" aria-haspopup="true">
        <svg class="pico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/>
        </svg>
        <span id="permName"></span>
      </button>
      <div class="pop" id="permPop" data-open="false"><div id="permList"></div></div>
    </div>`
}

beforeEach(() => {
  const shell: Shell = {
    T: (key) => key,
    confirmAsk: () => {},
    showPage: () => {},
  }
  window.RavenShell = shell
  markup()
})

afterEach(() => {
  delete window.RavenShell
  document.body.innerHTML = ''
  localStorage.clear()
})

const chip = (): HTMLElement => document.getElementById('permChip')!
const pop = (): HTMLElement => document.getElementById('permPop')!
const rows = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('#permList .prow')]

describe('the permission chip', () => {
  it('defaults to full access, which is what Raven actually does today', async () => {
    const perm = await load()
    expect(perm.current()).toBe('full')
    perm.draw()
    expect(document.getElementById('permName')!.textContent).toBe('gui.perm.full')
    /* The risky tier is marked as such on the chip, not just in the panel. */
    expect(chip().classList.contains('risk')).toBe(true)
    expect(chip().getAttribute('aria-label')).toBe('gui.perm.title: gui.perm.full')
  })

  it('restores a stored tier, and drops the risk mark with it', async () => {
    const perm = await load('smart')
    expect(perm.current()).toBe('smart')
    perm.draw()
    expect(document.getElementById('permName')!.textContent).toBe('gui.perm.smart')
    expect(chip().classList.contains('risk')).toBe(false)
  })

  it('ignores a stored tier no build offers any more', async () => {
    const perm = await load('godmode')
    expect(perm.current()).toBe('full')
  })

  it('puts the tier icon in the chip slot', async () => {
    const perm = await load('ask')
    perm.draw()
    const pic = chip().querySelector('.pico')!
    expect(pic.innerHTML).toContain('M9.6 10.2')
    expect(pic.querySelectorAll('path').length).toBe(3)
  })
})

describe('the permission panel', () => {
  it('lists the three tiers strictest first, as a radio group', async () => {
    const perm = await load()
    perm.open()
    expect(pop().dataset.open).toBe('true')
    expect(chip().getAttribute('aria-expanded')).toBe('true')
    expect(rows().map((r) => r.querySelector('.nm')!.textContent)).toEqual([
      'gui.perm.ask',
      'gui.perm.smart',
      'gui.perm.full',
    ])
    expect(rows().map((r) => r.getAttribute('role'))).toEqual(['radio', 'radio', 'radio'])
    expect(rows().map((r) => r.getAttribute('aria-checked'))).toEqual(['false', 'false', 'true'])
    /* Only the risky tier wears the class, and only the chosen one has a tick. */
    expect(rows().filter((r) => r.classList.contains('risk')).length).toBe(1)
    expect(pop().querySelectorAll('svg.tick').length).toBe(1)
  })

  it('reparents the panel to the body so fixed positioning means the viewport', async () => {
    const perm = await load()
    expect(pop().parentElement!.className).toBe('card')
    perm.open()
    /* The composer card animates, which makes it a containing block and quietly
       re-bases position: fixed against it. */
    expect(pop().parentElement).toBe(document.body)
    expect(pop().style.position).toBe('fixed')
    expect(pop().style.zIndex).toBe('46')
    expect(pop().style.right).toBe('auto')
    expect(pop().style.bottom).toBe('auto')
  })

  it('clamps its own left edge into the viewport', async () => {
    const perm = await load()
    perm.open()
    /* happy-dom measures everything as zero, so the useful assertion is the
       floor: the panel never lands at a negative offset. */
    expect(parseFloat(pop().style.left)).toBeGreaterThanOrEqual(8)
    expect(parseFloat(pop().style.top)).toBeGreaterThanOrEqual(8)
  })

  it('picks a tier, stores it, repaints the chip and closes', async () => {
    const perm = await load()
    perm.open()
    rows()[0]!.click()
    expect(perm.current()).toBe('ask')
    expect(localStorage.getItem('raven.perm')).toBe('ask')
    expect(document.getElementById('permName')!.textContent).toBe('gui.perm.ask')
    expect(chip().classList.contains('risk')).toBe(false)
    expect(pop().dataset.open).toBe('false')
    expect(chip().getAttribute('aria-expanded')).toBe('false')
  })

  it('rebuilds the list on each open, so the tick follows the pick', async () => {
    const perm = await load()
    perm.open()
    rows()[1]!.click()
    perm.open()
    expect(rows().map((r) => r.getAttribute('aria-checked'))).toEqual(['false', 'true', 'false'])
    expect(rows()[1]!.querySelector('svg.tick')).toBeTruthy()
    expect(pop().querySelectorAll('svg.tick').length).toBe(1)
  })

  it('toggles, since the panel has no close button of its own', async () => {
    const perm = await load()
    perm.toggle()
    expect(perm.isOpen()).toBe(true)
    perm.toggle()
    expect(perm.isOpen()).toBe(false)
  })

  it('survives a storage that refuses to be written', async () => {
    const perm = await load()
    const real = localStorage.setItem.bind(localStorage)
    localStorage.setItem = () => {
      throw new Error('private mode')
    }
    perm.open()
    expect(() => rows()[0]!.click()).not.toThrow()
    /* The pick still applies for this session; only its persistence is lost. */
    expect(perm.current()).toBe('ask')
    localStorage.setItem = real
  })

  it('does nothing at all when the markup is not in the document', async () => {
    const perm = await load()
    document.body.innerHTML = ''
    expect(() => {
      perm.draw()
      perm.open()
      perm.close()
    }).not.toThrow()
    expect(perm.isOpen()).toBe(false)
  })
})
