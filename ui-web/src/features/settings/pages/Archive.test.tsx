// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as confirm from '../../../state/confirm'
import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, snap, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'
import { AUTO_ARCHIVE_DAYS } from './Archive'

import type { ArchivedSession } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))

const railRefreshes: number[] = []
vi.mock('../../../state/session/registry', () => ({ refreshList: async () => { railRefreshes.push(1) } }))

const rows: ArchivedSession[] = [
  { id: 'tui:1', title: 'Quarterly deck', preview: 'p', last_message_preview: 'l', message_count: 3, started_at: 1, updated_at: Math.floor(Date.now() / 1000) - 3 * 86400 },
  { id: 'tui:2', title: '', preview: 'untitled one', last_message_preview: 'l', message_count: 1, started_at: 1, updated_at: Math.floor(Date.now() / 1000) - 40 * 86400 },
]


beforeEach(() => {
  railRefreshes.length = 0
  setSources({ settings: settingsSource, model: modelSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
})

describe('archive page', () => {
  it('lists the archived sessions by title, falling back to the preview', async () => {
    install(undefined, { archived: async () => rows })
    await mount('archive')
    expect(screen.getByText('Quarterly deck')).toBeTruthy()
    expect(screen.getByText('untitled one')).toBeTruthy()
  })

  it('restore asks the source and reloads the list; delete asks first and then removes', async () => {
    let listed = rows
    const { calls } = install(undefined, {
      archived: async () => { calls.push(['archived', null]); return listed },
      restore: async (id) => { calls.push(['restore', id]); listed = listed.filter((r) => r.id !== id) },
      removeSession: async (id) => { calls.push(['removeSession', id]); listed = listed.filter((r) => r.id !== id) },
    })
    await mount('archive')
    await act(async () => { fireEvent.click(screen.getAllByText('gui.settings.archive.restore')[0]!) })
    expect(calls.filter(([m]) => m !== 'archived')).toEqual([['restore', 'tui:1']])
    expect(calls.filter(([m]) => m === 'archived')).toHaveLength(2)
    expect(screen.queryByText('Quarterly deck')).toBeNull()
    const asked: string[] = []
    vi.spyOn(confirm, 'ask').mockImplementation((title, _b, _l, fn) => { asked.push(title); fn() })
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.archive.delete')) })
    expect(asked).toEqual(['gui.settings.archive.delete_title {"title":"untitled one"}'])
    expect(calls[calls.length - 2]).toEqual(['removeSession', 'tui:2'])
    expect(screen.getByText('gui.settings.archive.empty')).toBeTruthy()
  })

  it('declining the delete leaves the row and writes nothing', async () => {
    const { calls } = install(undefined, { archived: async () => rows })
    await mount('archive')
    vi.spyOn(confirm, 'ask').mockImplementation(() => {})
    await act(async () => { fireEvent.click(screen.getAllByText('gui.settings.archive.delete')[0]!) })
    expect(calls.some(([m]) => m === 'removeSession')).toBe(false)
    expect(screen.getByText('Quarterly deck')).toBeTruthy()
  })

  it('the auto-archive switch writes 30 on and null off', async () => {
    const { calls } = install(undefined, { archived: async () => [] })
    await mount('archive')
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'gui.settings.archive.auto' })) })
    expect(calls.filter(([m]) => m === 'set')).toEqual([['set', { key: 'sessions.autoArchiveAfterDays', value: AUTO_ARCHIVE_DAYS }]])
    cleanup()
    store._resetForTests()
    const on = snap({ raw: { ...snap().raw, sessions: { autoArchiveAfterDays: 30 } } })
    const second = install(on, { archived: async () => [] })
    await mount('archive')
    const sw = screen.getByRole('switch', { name: 'gui.settings.archive.auto' })
    expect(sw.getAttribute('aria-checked')).toBe('true')
    await act(async () => { fireEvent.click(sw) })
    expect(second.calls.filter(([m]) => m === 'set')).toEqual([['set', { key: 'sessions.autoArchiveAfterDays', value: null }]])
  })

  it('turning the switch on re-reads both lists, which is what runs the sweep', async () => {
    /* The sweep lives inside session.list, so the write alone moves nothing:
       the card went on saying nothing was archived and the rail went on showing
       the stale conversations, until a reload made a pile of them vanish at
       once. The two reads are the sweep and its result. */
    const { calls } = install(undefined, { archived: async () => { calls.push(['archived', null]); return [] } })
    await mount('archive')
    calls.length = 0
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'gui.settings.archive.auto' })) })
    expect(calls.map(([m]) => m)).toEqual(['set', 'archived'])
    expect(railRefreshes).toEqual([1])
  })

  it('a refused write re-reads nothing', async () => {
    /* The control: nothing was swept, so re-listing would only say so twice --
       and a green refresh after a failed write reads as though it took. */
    const { calls } = install(undefined, {
      archived: async () => { calls.push(['archived', null]); return [] },
      set: async () => { throw new Error('nope') },
    })
    await mount('archive')
    calls.length = 0
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'gui.settings.archive.auto' })) })
    expect(calls.some(([m]) => m === 'archived')).toBe(false)
    expect(railRefreshes).toEqual([])
  })
})
