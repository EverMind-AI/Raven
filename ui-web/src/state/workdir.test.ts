// @vitest-environment happy-dom
/* The workspace (state/workdir.ts), as the page draws it: the chip on a
   draft's bar (src/chrome/WorkdirChip.tsx) naming the default or the folder
   picked, the popover off it (src/chrome/WorkdirPopover.tsx) with default /
   recent folders / the host's folder dialog or the in-page walk, the tag
   beside a conversation's title
   (src/chrome/WorkdirTag.tsx) that names the folder it runs in, how a pick is
   staged for the create and dropped with the draft, the walk through the
   workspace source's listing with a folder the engine would refuse greyed and
   explained, and the two notes for a page with nothing to browse or a listing
   that failed. */
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import { _resetForTests as resetSession, setCurrent } from '../lib/session'
import { mountPageRoot } from '../test/pageRoot'
import { resetSources, setSources } from './sources'
import * as wd from './workdir'

import type { RailSource, SessRow } from '../features/rail/types'
import type { DirListing, WorkspaceSource } from '../features/workspace/types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let unmount = (): void => {}
let rows: SessRow[] = []

function rail(next: SessRow[]): void {
  rows = next
  setSources({
    rail: {
      snapshot: () => ({ rows, cur: null, busy: false }),
      replace: () => {},
      open: () => {},
    } satisfies RailSource,
  })
}

const row = (id: string, workdir: string | null): SessRow => ({ id, title: id, workdir, persisted: true })

/* A gateway elsewhere: it may offer the walk, but no dialog of its own can
   reach the reader, so the menu's last row is the in-page browser. */
function workspace(dirs?: WorkspaceSource['dirs']): void {
  setSources({
    workspace: {
      shortPath: (p) => p,
      hostPlatform: () => 'darwin',
      hostIsLocal: () => false,
      ...(dirs ? { dirs } : {}),
    } satisfies WorkspaceSource,
  })
}

/* The gateway on this desktop, with its folder dialog: the menu's last row
   opens that instead. */
function desktop(pickDir: WorkspaceSource['pickDir']): void {
  setSources({
    workspace: {
      shortPath: (p) => p,
      hostPlatform: () => 'darwin',
      hostIsLocal: () => true,
      dirs: async () => ({ path: '/home/me', parent: '/home', home: '/home/me', ok: true, entries: [] }),
      pickDir,
    } satisfies WorkspaceSource,
  })
}

beforeEach(() => {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  rail([])
  unmount = mountPageRoot()
})

afterEach(() => {
  act(() => { unmount() })
  unmount = () => {}
  wd._resetForTests()
  resetSession()
  resetSources()
  resetTranslator()
  document.body.innerHTML = ''
})

const chip = (): HTMLButtonElement => document.getElementById('wdChip') as HTMLButtonElement
/* The wrapper the chip shares with its popover, which is what hides the pair. */
const anchor = (): HTMLElement => chip().closest('.chrome-anch') as HTMLElement
const name = (): HTMLElement => document.getElementById('wdName')!
const tag = (): HTMLElement => document.getElementById('wdTag')!
const pop = (): HTMLElement => document.getElementById('wdPop')!
const prows = (): HTMLButtonElement[] => [...pop().querySelectorAll<HTMLButtonElement>('.prow')]
const byName = (text: string): HTMLButtonElement => prows().find((r) => r.querySelector('.nm')?.textContent === text)!
const settle = async (): Promise<void> => { await act(async () => { await Promise.resolve(); await Promise.resolve() }) }
const openFolders = (): void => {
  act(() => wd.draw())
  act(() => chip().click())
}

describe('the workspace a draft picks', () => {
  it('names the default, then the folder by its last segment once picked', () => {
    act(() => wd.draw())
    expect(anchor().hidden).toBe(false)
    expect(name().textContent).toBe('gui.wd.none')
    expect(chip().className).toBe('chip')
    expect(wd.get().paint).toMatchObject({ label: 'gui.wd.none', title: 'gui.wd.none_h', set: false, locked: false })
    expect(wd.staged()).toBeNull()
    expect(tag().hidden).toBe(true)

    act(() => wd.pick('/Users/me/proj/'))
    expect(wd.staged()).toBe('/Users/me/proj/')
    expect(name().textContent).toBe('proj')
    expect(chip().title).toBe('/Users/me/proj/')
    expect(chip().className).toBe('chip chrome-wd-set')
    expect(wd.base('C:\\work\\thesis')).toBe('thesis')
    expect(wd.base('/')).toBe('/')
    /* Still a draft: the tag is a conversation's. */
    expect(tag().hidden).toBe(true)
  })

  it('goes in a conversation, and the tag beside the title names the folder or the default instead', () => {
    rail([row('s1', '/w/thesis'), row('s2', null)])
    setCurrent('s1')
    act(() => wd.draw())
    expect(wd.get().paint).toMatchObject({ label: 'thesis', title: '/w/thesis', set: true, locked: true })
    expect(anchor().hidden).toBe(true)
    expect(tag().hidden).toBe(false)
    expect(tag().textContent).toBe('thesis')
    expect(tag().title).toBe('/w/thesis')
    expect(tag().getAttribute('aria-label')).toBe('gui.wd.title: thesis')
    expect(tag().className).toBe('chrome-wd-tag')
    /* A click that arrives anyway raises nothing. */
    act(() => wd.toggle())
    expect(wd.isOpen()).toBe(false)

    /* The default folder is said too, in the quieter face, so a reader is
       never left wondering whether no tag means the default or a miss. */
    setCurrent('s2')
    act(() => wd.draw())
    expect(wd.get().paint).toMatchObject({ label: 'gui.wd.none', title: 'gui.wd.none_h', set: false, locked: true })
    expect(anchor().hidden).toBe(true)
    expect(tag().hidden).toBe(false)
    expect(tag().textContent).toBe('gui.wd.none')
    expect(tag().className).toBe('chrome-wd-tag chrome-wd-default')
  })

  it('opens off the chip with the default ticked, a rule, the recent folders once each, and the browse row', () => {
    rail([row('a', '/w/alpha'), row('b', null), row('c', '/w/alpha'), row('d', '/w/beta')])
    openFolders()
    expect(pop().dataset.open).toBe('true')
    expect(pop().dataset.view).toBe('menu')
    expect(chip().getAttribute('aria-expanded')).toBe('true')
    expect(pop().parentElement).toBe(anchor())
    expect(prows().map((r) => r.querySelector('.nm')?.textContent)).toEqual(['gui.wd.none', 'alpha', 'beta', 'gui.wd.open_remote'])
    expect(byName('gui.wd.none').getAttribute('aria-checked')).toBe('true')
    /* Names only; the path is on the row's title, and the rule stands between
       the default and the folders. */
    expect(byName('alpha').title).toBe('/w/alpha')
    expect(pop().querySelector('.sub, .hd')).toBeNull()
    expect(byName('gui.wd.none').nextElementSibling!.className).toBe('chrome-hr')

    act(() => byName('beta').click())
    expect(wd.staged()).toBe('/w/beta')
    expect(name().textContent).toBe('beta')
    /* A pick takes the popover down, and the chip is what says what was picked. */
    expect(pop().dataset.open).toBe('false')

    /* Back to the default takes the staged pick off again. */
    openFolders()
    expect(byName('beta').getAttribute('aria-checked')).toBe('true')
    act(() => byName('gui.wd.none').click())
    expect(wd.staged()).toBeNull()
    expect(name().textContent).toBe('gui.wd.none')
  })

  it('draws no rule when there is nothing under the default', () => {
    openFolders()
    expect(prows().map((r) => r.querySelector('.nm')?.textContent)).toEqual(['gui.wd.none', 'gui.wd.open_remote'])
    expect(pop().querySelector('.chrome-hr')).toBeNull()
  })

  it('drops the pick with the draft, and the popover with it', () => {
    act(() => wd.pick('/w/beta'))
    openFolders()
    expect(pop().dataset.open).toBe('true')
    act(() => wd.clearStaged())
    expect(wd.staged()).toBeNull()
    expect(name().textContent).toBe('gui.wd.none')
    expect(pop().dataset.open).toBe('false')
  })

  it('closes when the reader leaves the draft for a conversation while it stands open', () => {
    /* The pick locks on the switch; a popover left standing over it would still
       take a click and stage a folder for some later draft. */
    rail([row('s1', '/w/thesis')])
    openFolders()
    expect(pop().dataset.open).toBe('true')
    setCurrent('s1')
    act(() => wd.draw())
    expect(wd.get().paint?.locked).toBe(true)
    expect(pop().dataset.open).toBe('false')
    expect(pop().querySelectorAll('.prow')).toHaveLength(0)
  })

  it('closes on a pointer landing outside it, and stays for one on the chip or inside', async () => {
    const { installGlobalListeners } = await import('./globalListeners')
    installGlobalListeners()
    openFolders()
    const down = (target: Element): void => {
      act(() => { target.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, composed: true })) })
    }
    down(chip())
    expect(pop().dataset.open).toBe('true')
    down(byName('gui.wd.open_remote'))
    expect(pop().dataset.open).toBe('true')
    down(document.getElementById('ta')!)
    expect(pop().dataset.open).toBe('false')
    expect(wd.isOpen()).toBe(false)
  })

  it('opens the desktop\'s own folder dialog where the gateway is this desktop, and stages what it answers', async () => {
    let asked = 0
    desktop(async () => { asked += 1; return { path: '/w/chosen', ok: true } })
    openFolders()
    expect(prows().map((r) => r.querySelector('.nm')?.textContent)).toEqual(['gui.wd.none', 'gui.wd.open'])
    act(() => byName('gui.wd.open').click())
    /* The menu goes down with the click -- the dialog it opened is the
       reader's window now -- and takes no second press while that one is up. */
    expect(pop().dataset.open).toBe('false')
    expect(wd.isPicking()).toBe(true)
    await settle()
    expect(asked).toBe(1)
    expect(wd.staged()).toBe('/w/chosen')
    expect(name().textContent).toBe('chosen')
    expect(pop().dataset.open).toBe('false')
    expect(wd.isPicking()).toBe(false)
  })

  it('leaves the folder alone when the dialog is dismissed, and says nothing', async () => {
    desktop(async () => ({ ok: false }))
    openFolders()
    act(() => byName('gui.wd.open').click())
    await settle()
    expect(wd.staged()).toBeNull()
    /* Nothing to report and nothing to go back to: a dismissal is an answer,
       so the menu that went down with the click stays down. */
    expect(pop().dataset.open).toBe('false')
    expect(wd.isPicking()).toBe(false)
    expect(pop().querySelector('.chrome-wd-err')).toBeNull()
  })

  it('declines a chosen folder the engine would refuse, and brings the menu back to say so', async () => {
    desktop(async () => ({ path: '/home/me/raven-home', ok: false }))
    openFolders()
    act(() => byName('gui.wd.open').click())
    expect(pop().dataset.open).toBe('false')
    await settle()
    expect(wd.staged()).toBeNull()
    expect(pop().dataset.open).toBe('true')
    expect(pop().querySelector('.chrome-wd-err')?.textContent).toBe('gui.wd.blocked')
  })

  it('drops a folder the dialog answers after the draft became a conversation', async () => {
    let release: (r: { path: string; ok: boolean }) => void = () => {}
    desktop(() => new Promise((resolve) => { release = resolve }))
    rail([row('s1', '/w/thesis')])
    openFolders()
    act(() => byName('gui.wd.open').click())
    setCurrent('s1')
    act(() => wd.draw())
    await act(async () => { release({ path: '/w/late', ok: true }); await Promise.resolve() })
    await settle()
    expect(wd.staged()).toBeNull()
  })

  it('reports a dialog that failed, on the menu', async () => {
    desktop(async () => { throw { data: { detail: 'no display' } } })
    openFolders()
    act(() => byName('gui.wd.open').click())
    await settle()
    expect(pop().querySelector('.chrome-wd-err')?.textContent).toBe('gui.wd.failed {"detail":"no display"}')
    expect(wd.isPicking()).toBe(false)
  })

  it('walks the listing and offers the folder only where the engine would take it', async () => {
    const asked: Array<string | undefined> = []
    /* Agent home is /srv/raven-home/workspace here, so /srv/raven-home is an
       ancestor: not a workspace itself, but its `projects` is a fine one. */
    const listings: Record<string, DirListing> = {
      '': { path: '/srv', parent: '/', home: '/home/me', ok: true,
        entries: [{ name: 'proj', path: '/srv/proj', ok: true }, { name: 'raven-home', path: '/srv/raven-home', ok: false }] },
      '/srv/raven-home': { path: '/srv/raven-home', parent: '/srv', home: '/home/me', ok: false,
        entries: [{ name: 'projects', path: '/srv/raven-home/projects', ok: true }, { name: 'workspace', path: '/srv/raven-home/workspace', ok: false }] },
      '/srv/raven-home/projects': { path: '/srv/raven-home/projects', parent: '/srv/raven-home', home: '/home/me', ok: true, entries: [] },
      '/home/me': { path: '/home/me', parent: '/home', home: '/home/me', ok: true, entries: [] },
    }
    workspace(async (p) => { asked.push(p); return listings[p || '']! })
    openFolders()
    act(() => byName('gui.wd.open_remote').click())
    await settle()
    expect(asked).toEqual([undefined])
    expect(wd.get().view).toBe('browse')
    expect(pop().dataset.view).toBe('browse')
    expect(pop().querySelector('.chrome-wd-p')?.getAttribute('title')).toBe('/srv')
    expect(prows().map((r) => r.querySelector('.nm')?.textContent)).toEqual(['proj', 'raven-home'])
    /* Marked and explained, but still a way in. */
    const anc = byName('raven-home')
    expect(anc.disabled).toBe(false)
    expect(anc.classList.contains('chrome-wd-off')).toBe(true)
    expect(anc.title).toBe('gui.wd.blocked')
    act(() => anc.click())
    await settle()
    expect(asked.at(-1)).toBe('/srv/raven-home')
    const use = (): HTMLButtonElement => pop().querySelector<HTMLButtonElement>('.chrome-wd-use')!
    expect(use().disabled).toBe(true)
    expect(use().title).toBe('gui.wd.blocked')
    expect(byName('projects').classList.contains('chrome-wd-off')).toBe(false)
    expect(byName('workspace').classList.contains('chrome-wd-off')).toBe(true)
    act(() => byName('projects').click())
    await settle()
    expect(pop().querySelector('.chrome-wd-list .note')?.textContent).toBe('gui.wd.empty')
    expect(use().disabled).toBe(false)
    /* Home and up. */
    const [home, up] = [...pop().querySelectorAll<HTMLButtonElement>('.chrome-wd-nav')]
    expect(up!.disabled).toBe(false)
    act(() => home!.click())
    await settle()
    expect(asked.at(-1)).toBe('/home/me')
    act(() => up!.click())
    await settle()
    expect(asked.at(-1)).toBe('/home')
  })

  it('stages the folder the browser stands in, and goes back to the menu', async () => {
    workspace(async () => ({ path: '/w/proj', parent: '/w', home: '/home/me', ok: true, entries: [] }))
    openFolders()
    act(() => byName('gui.wd.open_remote').click())
    await settle()
    act(() => pop().querySelector<HTMLButtonElement>('.chrome-wd-use')!.click())
    expect(wd.staged()).toBe('/w/proj')
    expect(name().textContent).toBe('proj')
    expect(pop().dataset.open).toBe('false')

    openFolders()
    expect(byName('proj').getAttribute('aria-checked')).toBe('true')
    act(() => byName('gui.wd.open_remote').click())
    await settle()
    expect(wd.get().view).toBe('browse')
    act(() => pop().querySelectorAll<HTMLButtonElement>('.chrome-wd-btn')[0]!.click())
    expect(wd.get().view).toBe('menu')
    expect(byName('gui.wd.none')).toBeTruthy()
  })

  it('says so when there is nothing to browse, and reports a refused listing in place', async () => {
    workspace()
    openFolders()
    act(() => byName('gui.wd.open_remote').click())
    await settle()
    expect(wd.get().view).toBe('menu')
    expect(pop().querySelector('.chrome-wd-err')?.textContent).toBe('gui.wd.not_live')

    workspace(async () => { throw { data: { detail: 'not a directory' } } })
    act(() => byName('gui.wd.open_remote').click())
    await settle()
    expect(pop().querySelector('.chrome-wd-err')?.textContent).toBe('gui.wd.failed {"detail":"not a directory"}')
    /* Still on the menu: the refusal did not replace it. */
    expect(byName('gui.wd.none')).toBeTruthy()
  })
})
