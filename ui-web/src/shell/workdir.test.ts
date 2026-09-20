// @vitest-environment happy-dom
/* The working-directory chip and its panel (shell/workdir.ts): the chip's two
   states (live on a draft, fixed and reporting once a conversation is open),
   the menu of no folder / recent folders / browse, how a pick is staged for the
   create and taken back off, the walk through the gateway's directory listing
   with entries the create would refuse greyed out, and the notes for a page
   with nothing to browse or a path the gateway refused. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Shell } from './bridge'
import type { DirListing } from './workdir'

async function load(): Promise<typeof import('./workdir')> {
  vi.resetModules()
  return import('./workdir')
}

/* The chip and the panel as page.html carries them, inside the composer card
   the panel has to clear. */
function markup(): void {
  document.body.innerHTML = `
    <div class="dock-in">
      <button class="chip" id="wdChip" aria-expanded="false" aria-haspopup="true">
        <svg class="pico" viewBox="0 0 24 24" aria-hidden="true"><path d=""/></svg>
        <span id="wdName"></span>
      </button>
      <div class="pop wd" id="wdPop" data-open="false"><div id="wdBody"></div></div>
    </div>`
}

interface ComposerSeam {
  stageWorkdir?: (dir: string | null) => void
  browseDirs?: (path?: string) => Promise<DirListing>
}
const composer: ComposerSeam = {}

beforeEach(() => {
  const shell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    confirmAsk: () => {},
    showPage: () => {},
  }
  window.RavenShell = shell
  window.DS = { composer, sessions: { snapshot: () => ({ rows: [], cur: null, busy: false }) } }
  markup()
})

afterEach(() => {
  delete window.RavenShell
  delete window.DS
  delete composer.stageWorkdir
  delete composer.browseDirs
  document.body.innerHTML = ''
})

const chip = (): HTMLButtonElement => document.getElementById('wdChip') as HTMLButtonElement
const name = (): HTMLElement => document.getElementById('wdName')!
const pop = (): HTMLElement => document.getElementById('wdPop')!
const rows = (): HTMLButtonElement[] => [...document.querySelectorAll<HTMLButtonElement>('#wdBody .prow')]
const byText = (text: string): HTMLButtonElement => rows().find((r) => r.querySelector('.nm')?.textContent === text)!

function withRows(dirs: Array<string | null>): void {
  window.DS = {
    composer,
    sessions: {
      snapshot: () => ({ rows: dirs.map((d, i) => ({ id: String(i), title: 't', workdir: d })), cur: null, busy: false }),
    },
  }
}

describe('the working-directory chip', () => {
  it('starts a draft on no folder, with the chip live', async () => {
    const wd = await load()
    wd.setDraft()
    expect(name().textContent).toBe('gui.wd.none')
    expect(chip().disabled).toBe(false)
    expect(wd.current()).toBeNull()
  })

  it('names the folder by its last segment, on either separator', async () => {
    const wd = await load()
    expect(wd.base('/Users/me/proj/')).toBe('proj')
    expect(wd.base('C:\\work\\thesis')).toBe('thesis')
    expect(wd.base('/')).toBe('/')
  })

  it('opens on the chip with no folder checked and the browse row, and stages a pick', async () => {
    const wd = await load()
    const staged: Array<string | null> = []
    composer.stageWorkdir = (d) => staged.push(d)
    withRows(['/w/alpha', null, '/w/alpha', '/w/beta'])
    wd.setDraft()
    wd.toggle()
    expect(pop().dataset.open).toBe('true')
    expect(chip().getAttribute('aria-expanded')).toBe('true')
    /* No folder, then the two recent folders each once, then browse. */
    expect(rows().map((r) => r.querySelector('.nm')?.textContent)).toEqual(['gui.wd.none', 'alpha', 'beta', 'gui.wd.open'])
    expect(byText('gui.wd.none').getAttribute('aria-checked')).toBe('true')
    expect(byText('alpha').querySelector('.sub')?.textContent).toBe('/w/alpha')

    byText('beta').click()
    expect(staged).toEqual(['/w/beta'])
    expect(wd.current()).toBe('/w/beta')
    expect(name().textContent).toBe('beta')
    expect(chip().classList.contains('set')).toBe(true)
    expect(pop().dataset.open).toBe('false')

    /* Back to no folder takes the staged pick off again. */
    wd.toggle()
    expect(byText('beta').getAttribute('aria-checked')).toBe('true')
    byText('gui.wd.none').click()
    expect(staged).toEqual(['/w/beta', null])
    expect(name().textContent).toBe('gui.wd.none')
  })

  it('reports and locks once a conversation is open, and clears on the next draft', async () => {
    const wd = await load()
    wd.setSession('/w/thesis')
    expect(name().textContent).toBe('thesis')
    expect(chip().disabled).toBe(true)
    expect(chip().title).toContain('/w/thesis')
    expect(chip().title).toContain('gui.wd.locked')
    wd.toggle()
    expect(pop().dataset.open).toBe('false')

    wd.setSession(null)
    expect(name().textContent).toBe('gui.wd.none')
    expect(chip().disabled).toBe(true)

    wd.setDraft()
    expect(chip().disabled).toBe(false)
    expect(wd.current()).toBeNull()
  })

  it('walks the gateway listing and offers the folder only where the engine would take it', async () => {
    const wd = await load()
    const asked: Array<string | undefined> = []
    /* Agent home is /srv/raven-home/workspace here, so /srv/raven-home is an
       ancestor: not a workspace itself, but its `projects` is a fine one. That
       is the one kind of row the gateway ever marks false, since it never
       lists dotted names. */
    const listings: Record<string, DirListing> = {
      '': { path: '/srv', parent: '/', home: '/home/me', ok: true,
        entries: [{ name: 'proj', path: '/srv/proj', ok: true }, { name: 'raven-home', path: '/srv/raven-home', ok: false }] },
      '/srv/proj': { path: '/srv/proj', parent: '/srv', home: '/home/me', ok: true, entries: [] },
      '/srv/raven-home': { path: '/srv/raven-home', parent: '/srv', home: '/home/me', ok: false,
        entries: [{ name: 'projects', path: '/srv/raven-home/projects', ok: true }, { name: 'workspace', path: '/srv/raven-home/workspace', ok: false }] },
      '/srv/raven-home/projects': { path: '/srv/raven-home/projects', parent: '/srv/raven-home', home: '/home/me', ok: true, entries: [] },
    }
    composer.browseDirs = async (p) => { asked.push(p); return listings[p || '']! }
    const staged: Array<string | null> = []
    composer.stageWorkdir = (d) => staged.push(d)
    wd.setDraft()
    wd.toggle()
    byText('gui.wd.open').click()
    await Promise.resolve(); await Promise.resolve()
    expect(asked).toEqual([undefined])
    expect(document.querySelector('.wdpath .p')?.textContent?.replace(/\u200e/g, '')).toBe('/srv')
    expect(document.querySelector('.wdpath .p')?.getAttribute('title')).toBe('/srv')
    expect(rows().map((r) => r.querySelector('.nm')?.textContent)).toEqual(['proj', 'raven-home'])
    /* Marked and explained, but still a way in: the folders under it may be
       perfectly good workspaces, and the panel offers no typed path to reach
       them any other way. */
    const anc = byText('raven-home')
    expect(anc.disabled).toBe(false)
    expect(anc.classList.contains('off')).toBe(true)
    expect(anc.title).toBe('gui.wd.blocked')
    anc.click()
    await Promise.resolve(); await Promise.resolve()
    expect(asked.at(-1)).toBe('/srv/raven-home')
    /* Inside it the pick is withheld, since the directory itself is refused. */
    const useHere = () => [...document.querySelectorAll<HTMLButtonElement>('.wdfoot button')].find((b) => b.textContent === 'gui.wd.use')!
    expect(useHere().disabled).toBe(true)
    expect(byText('projects').classList.contains('off')).toBe(false)
    expect(byText('workspace').classList.contains('off')).toBe(true)
    byText('projects').click()
    await Promise.resolve(); await Promise.resolve()
    expect(asked.at(-1)).toBe('/srv/raven-home/projects')
    expect(document.querySelector('.wdlist .note')?.textContent).toBe('gui.wd.empty')
    expect(useHere().disabled).toBe(false)
    useHere().click()
    expect(staged).toEqual(['/srv/raven-home/projects'])
    expect(name().textContent).toBe('projects')
    expect(pop().dataset.open).toBe('false')
  })

  it('greys out "use this folder" inside the agent data, and goes up and home', async () => {
    const wd = await load()
    const asked: Array<string | undefined> = []
    const inside: DirListing = { path: '/home/me/.raven', parent: '/home/me', home: '/home/me', ok: false, entries: [] }
    const top: DirListing = { path: '/', parent: null, home: '/home/me', ok: true, entries: [] }
    composer.browseDirs = async (p) => { asked.push(p); return p === '/' ? top : inside }
    wd.setDraft()
    wd.toggle()
    byText('gui.wd.open').click()
    await Promise.resolve(); await Promise.resolve()
    const use = [...document.querySelectorAll<HTMLButtonElement>('.wdfoot button')].find((b) => b.textContent === 'gui.wd.use')!
    expect(use.disabled).toBe(true)
    expect(use.title).toBe('gui.wd.blocked')
    const [home, up] = [...document.querySelectorAll<HTMLButtonElement>('.wdpath .icb')]
    expect(up!.disabled).toBe(false)
    up!.click()
    await Promise.resolve(); await Promise.resolve()
    expect(asked.at(-1)).toBe('/home/me')
    home!.click()
    await Promise.resolve(); await Promise.resolve()
    expect(asked.at(-1)).toBe('/home/me')
    const back = [...document.querySelectorAll<HTMLButtonElement>('.wdfoot button')].find((b) => b.textContent === 'gui.wd.back')!
    back.click()
    expect(rows()[0]!.querySelector('.nm')?.textContent).toBe('gui.wd.none')
  })

  it('says so when there is nothing to browse, and reports a refused path in place', async () => {
    const wd = await load()
    wd.setDraft()
    wd.toggle()
    byText('gui.wd.open').click()
    await Promise.resolve()
    expect(document.querySelector('#wdBody .note.err')?.textContent).toBe('gui.wd.not_live')

    composer.browseDirs = async () => { throw { data: { detail: 'not a directory' } } }
    byText('gui.wd.open').click()
    await Promise.resolve(); await Promise.resolve()
    expect(document.querySelector('#wdBody .note.err')?.textContent).toBe('gui.wd.failed {"detail":"not a directory"}')
    /* Still on the menu: the refusal did not replace it. */
    expect(byText('gui.wd.none')).toBeTruthy()
  })
})
