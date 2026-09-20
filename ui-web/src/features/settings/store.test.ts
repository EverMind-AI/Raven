// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as settingsDialog from '../../state/settings'
import { resetSources, setSources } from '../../state/sources'
import * as store from './store'

import type { SettingsSnapshot, SettingsSource } from './types'

const snapOf = (model: string): SettingsSnapshot => ({
  raw: {}, configPath: '', everos: null, providers: [], curProvider: '', model, tools: [], skills: [], mcp: [],
})

afterEach(() => {
  store._resetForTests()
  settingsDialog._resetForTests()
  resetSources()
  vi.restoreAllMocks()
})

describe('settings store', () => {
  it('exposes the store shape and notifies on every write', () => {
    let n = 0
    const off = store.subscribe(() => { n += 1 })
    store.set({ tab: 'usage' })
    expect(store.get().tab).toBe('usage')
    expect(n).toBe(1)
    store.redraw()
    expect(n).toBe(2)
    off()
    store.set({ err: 'x' })
    expect(n).toBe(2)
  })

  it('setTab closes every drawer of the section it leaves', () => {
    store.set({ provider: 'openai', sheet: { slug: 'openai', q: '', sel: [], state: 'ready', items: [], kind: 'all', folded: {}, typed: null }, skill: 's', toolOpen: 'exec', plugOpen: 'p', err: 'oops' })
    store.setTab('tools')
    const s = store.get()
    expect(s.tab).toBe('tools')
    /* The model picker is no longer one of them: it is the composer's, owned by
       the model store, and it closes on its own when the dialog does. */
    expect([s.provider, s.sheet, s.skill, s.toolOpen, s.plugOpen, s.err]).toEqual([null, null, null, null, null, ''])
  })

  it('both model doors open the providers page, which is where a key and a list are', async () => {
    /* `openModels` answers "nothing is connected" and `openProviderModels`
       answers "this one has no model added". Since the split, neither question
       is answerable on the Model settings page: it holds the roles card and no
       way to connect anything. */
    setSources({ settings: { load: async () => snapOf('m') } as unknown as SettingsSource })
    vi.spyOn(settingsDialog, 'open').mockImplementation(() => {})
    await store.openModels()
    expect(settingsDialog.settingsTab.id).toBe('provider')
    settingsDialog.settingsTab.id = 'general'
    await store.openProviderModels('moonshot')
    expect([settingsDialog.settingsTab.id, store.get().provider]).toEqual(['provider', 'moonshot'])
  })

  it('run marks the key busy while the write runs and lands the snapshot it returns', async () => {
    let release: (v: SettingsSnapshot) => void = () => {}
    const p = store.run('k', () => new Promise<SettingsSnapshot>((r) => { release = r }))
    expect(store.isBusy('k')).toBe(true)
    release(snapOf('after'))
    expect(await p).toBe(true)
    expect(store.isBusy('k')).toBe(false)
    expect(store.get().snap.model).toBe('after')
  })

  it('run resolves false on a refusal the source already handled, leaving the snapshot alone', async () => {
    store.set({ snap: snapOf('before') })
    const ok = await store.run('k', () => Promise.reject({ handled: true }))
    expect(ok).toBe(false)
    expect(store.get().snap.model).toBe('before')
    expect(store.isBusy('k')).toBe(false)
  })

  it('write goes through the source with the key and value', async () => {
    const calls: unknown[] = []
    setSources({ settings: { set: async (key: string, value: unknown) => { calls.push([key, value]); return snapOf('m') } } as unknown as SettingsSource })
    await store.write('tools.disabledTools', ['exec'])
    expect(calls).toEqual([['tools.disabledTools', ['exec']]])
  })

  it('lastDays spans today and the days before it, inclusive', () => {
    const r = store.lastDays(7)
    const from = new Date(`${r.from}T00:00:00`)
    const to = new Date(`${r.to}T00:00:00`)
    expect(Math.round((to.getTime() - from.getTime()) / 86400000)).toBe(6)
    expect(r.to).toBe(store.isoDay(new Date()))
    expect(store.lastDays(1)).toEqual({ from: r.to, to: r.to })
  })

  it('usageLoad keeps the newest range when an older answer lands late', async () => {
    let releaseFirst: (v: null) => void = () => {}
    const first = { kind: '7', from: '2026-09-01', to: '2026-09-07' }
    const second = { kind: '1', from: '2026-09-18', to: '2026-09-18' }
    setSources({ settings: {
      usage: async (range: { kind?: string }) => (range === first ? new Promise<null>((r) => { releaseFirst = r }) : null),
    } as unknown as SettingsSource })
    const a = store.usageLoad(first)
    await store.usageLoad(second)
    expect(store.get().range).toBe(second)
    expect(store.get().usage).toBe(null)
    store.set({ usage: undefined })
    releaseFirst(null)
    await a
    expect(store.get().usage).toBe(undefined)
  })
})

describe('settings store, what a reopen drops', () => {
  it('opening again clears what the pages fetched for themselves', async () => {
    setSources({ settings: { load: async () => snapOf('m') } as unknown as SettingsSource })
    /* Both carry an answer from an earlier open. `refresh` reloads the
       snapshot and cannot touch these two, so without the clear a dialog
       opened once showed its first answer for the life of the page -- a
       session archived from the rail in between never reached the archive
       page, and the usage totals stayed at whatever they were on first open. */
    store.set({ usage: null, archived: [] })
    await store.open()
    expect(store.get().usage).toBe(undefined)
    expect(store.get().archived).toBe(null)
    settingsDialog.close()
  })
})

describe('settings store, the inventory push', () => {
  it('refreshSoon reloads once per burst while the dialog is open, and not at all while it is down', async () => {
    vi.useFakeTimers()
    const loads: number[] = []
    setSources({ settings: { load: async () => { loads.push(1); return snapOf('pushed') } } as unknown as SettingsSource })
    store.set({ loaded: true })
    store.refreshSoon()
    await vi.advanceTimersByTimeAsync(store.REFRESH_SOON_MS + 50)
    expect(loads).toHaveLength(0)
    settingsDialog.open()
    store.refreshSoon()
    store.refreshSoon()
    store.refreshSoon()
    await vi.advanceTimersByTimeAsync(store.REFRESH_SOON_MS + 50)
    expect(loads).toHaveLength(1)
    expect(store.get().snap.model).toBe('pushed')
    settingsDialog.close()
    vi.useRealTimers()
  })
})
