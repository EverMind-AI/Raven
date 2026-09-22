// @vitest-environment happy-dom
/* The working-directory chip and its popover (state/workdir.ts), rendered by
   the page root's <WorkdirChip/> and <WorkdirPopover/>: the chip's two states
   (live on a draft, a disabled report in a conversation), the word for no pick,
   the menu of default / recent folders / browse, how a pick is staged for the
   create and dropped with the draft, the walk through the workspace source's
   listing with a folder the engine would refuse greyed and explained, and the
   two notes for a page with nothing to browse or a listing that failed. */
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

function workspace(dirs?: WorkspaceSource['dirs']): void {
  setSources({
    workspace: {
      shortPath: (p) => p,
      hostPlatform: () => 'darwin',
      ...(dirs ? { dirs } : {}),
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
const name = (): HTMLElement => document.getElementById('wdName')!
const pop = (): HTMLElement => document.getElementById('wdPop')!
const prows = (): HTMLButtonElement[] => [...pop().querySelectorAll<HTMLButtonElement>('.prow')]
const byName = (text: string): HTMLButtonElement => prows().find((r) => r.querySelector('.nm')?.textContent === text)!
const settle = async (): Promise<void> => { await act(async () => { await Promise.resolve(); await Promise.resolve() }) }

describe('the working-directory chip', () => {
  it('says "default" on a draft, live, and names the folder by its last segment once picked', () => {
    act(() => wd.draw())
    expect(name().textContent).toBe('gui.wd.none')
    expect(chip().title).toBe('gui.wd.none_h')
    expect(chip().disabled).toBe(false)
    expect(chip().classList.contains('chrome-wd-set')).toBe(false)
    expect(wd.staged()).toBeNull()

    act(() => wd.pick('/Users/me/proj/'))
    expect(wd.staged()).toBe('/Users/me/proj/')
    expect(name().textContent).toBe('proj')
    expect(chip().title).toBe('/Users/me/proj/')
    expect(chip().classList.contains('chrome-wd-set')).toBe(true)
    expect(wd.base('C:\\work\\thesis')).toBe('thesis')
    expect(wd.base('/')).toBe('/')
  })

  it('reports and locks in a conversation, and reads the folder off the row', () => {
    rail([row('s1', '/w/thesis'), row('s2', null)])
    setCurrent('s1')
    act(() => wd.draw())
    expect(name().textContent).toBe('thesis')
    expect(chip().disabled).toBe(true)
    expect(chip().title).toBe('/w/thesis\ngui.wd.locked')
    /* A click that arrives anyway raises nothing. */
    act(() => wd.toggle())
    expect(pop().dataset.open).toBe('false')

    setCurrent('s2')
    act(() => wd.draw())
    expect(name().textContent).toBe('gui.wd.none')
    expect(chip().disabled).toBe(true)
    expect(chip().title).toBe('gui.wd.none_h\ngui.wd.locked')
  })

  it('opens on the chip with the default ticked, the recent folders once each, and the browse row', () => {
    rail([row('a', '/w/alpha'), row('b', null), row('c', '/w/alpha'), row('d', '/w/beta')])
    act(() => wd.draw())
    act(() => chip().click())
    expect(pop().dataset.open).toBe('true')
    expect(chip().getAttribute('aria-expanded')).toBe('true')
    expect(prows().map((r) => r.querySelector('.nm')?.textContent)).toEqual(['gui.wd.none', 'alpha', 'beta', 'gui.wd.open'])
    expect(byName('gui.wd.none').getAttribute('aria-checked')).toBe('true')
    expect(byName('alpha').querySelector('.sub')?.textContent?.replace(/\u200e/g, '')).toBe('/w/alpha')

    act(() => byName('beta').click())
    expect(wd.staged()).toBe('/w/beta')
    expect(name().textContent).toBe('beta')
    expect(pop().dataset.open).toBe('false')

    /* Back to the default takes the staged pick off again. */
    act(() => chip().click())
    expect(byName('beta').getAttribute('aria-checked')).toBe('true')
    act(() => byName('gui.wd.none').click())
    expect(wd.staged()).toBeNull()
    expect(name().textContent).toBe('gui.wd.none')
  })

  it('drops the pick with the draft', () => {
    act(() => wd.pick('/w/beta'))
    act(() => wd.clearStaged())
    expect(wd.staged()).toBeNull()
    expect(name().textContent).toBe('gui.wd.none')
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
    act(() => wd.draw())
    act(() => chip().click())
    act(() => byName('gui.wd.open').click())
    await settle()
    expect(asked).toEqual([undefined])
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
    act(() => wd.draw())
    act(() => chip().click())
    act(() => byName('gui.wd.open').click())
    await settle()
    act(() => pop().querySelector<HTMLButtonElement>('.chrome-wd-use')!.click())
    expect(wd.staged()).toBe('/w/proj')
    expect(name().textContent).toBe('proj')
    expect(pop().dataset.open).toBe('false')

    act(() => chip().click())
    expect(byName('proj').getAttribute('aria-checked')).toBe('true')
    act(() => byName('gui.wd.open').click())
    await settle()
    expect(pop().dataset.view).toBe('browse')
    act(() => pop().querySelectorAll<HTMLButtonElement>('.chrome-wd-btn')[0]!.click())
    expect(pop().dataset.view).toBe('menu')
    expect(byName('gui.wd.none')).toBeTruthy()
  })

  it('says so when there is nothing to browse, and reports a refused listing in place', async () => {
    workspace()
    act(() => wd.draw())
    act(() => chip().click())
    act(() => byName('gui.wd.open').click())
    await settle()
    expect(pop().dataset.view).toBe('menu')
    expect(pop().querySelector('.chrome-wd-err')?.textContent).toBe('gui.wd.not_live')

    workspace(async () => { throw { data: { detail: 'not a directory' } } })
    act(() => byName('gui.wd.open').click())
    await settle()
    expect(pop().querySelector('.chrome-wd-err')?.textContent).toBe('gui.wd.failed {"detail":"not a directory"}')
    /* Still on the menu: the refusal did not replace it. */
    expect(byName('gui.wd.none')).toBeTruthy()
  })
})
