// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'
import { lastDays } from '../store'
import { MAX_DAYS, clampRange } from './Usage'

import type { UsageStats } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const toasts = vi.hoisted(() => ({ calls: [] as string[] }))
vi.mock('../../../state/toast', () => ({ show: (t: string) => { toasts.calls.push(t) }, subscribe: () => () => {}, get: () => [] }))

const ZERO = { cost_missing_calls: 0, cache_read_missing_calls: 0, cache_write_missing_calls: 0, legacy_cost_calls: 0 }

function stats(days: string[], withWrite = false): UsageStats {
  return {
    days: days.length, from: days[0]!, to: days[days.length - 1]!,
    daily: days.map((date, i) => ({ date, calls: i, cost_usd: i * 0.5, ...ZERO })),
    llm: {
      total: { calls: 12, input_tokens: 800, output_tokens: 300, cache_read_tokens: 200, cost_usd: 1.75, ...ZERO },
      models: [
        { model: 'priced', calls: 10, input_tokens: 700, cache_read_tokens: 200, output_tokens: 250, cost_usd: 1.5, cache_write_tokens: withWrite ? 40 : 0, ...ZERO },
        { model: 'free', calls: 2, input_tokens: 100, cache_read_tokens: 0, output_tokens: 50, cost_usd: null, ...ZERO },
      ],
    },
    tools: { total: 3, counts: [{ name: 'exec', count: 2 }, { name: 'read_file', count: 1 }] },
  }
}


beforeEach(() => {
  setSources({ settings: settingsSource, model: modelSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
  toasts.calls = []
})

describe('usage page', () => {
  it('asks for the last 30 days on open and for 7 on the pick, today and the six before', async () => {
    const { calls } = install(undefined, { usage: async (range) => { calls.push(['usage', range]); return stats([range.from, range.to]) } })
    await mount('usage')
    expect(calls[0]).toEqual(['usage', { kind: '30', ...lastDays(30) }])
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.usage.days {"n":7}')) })
    expect(calls[1]).toEqual(['usage', { kind: '7', ...lastDays(7) }])
  })

  it('draws one bar per day, the tiles from the totals, no price where cost is null, and the write column only when written', async () => {
    const days = ['2026-09-10', '2026-09-11', '2026-09-12']
    install(undefined, { usage: async () => stats(days) })
    await mount('usage')
    expect(document.querySelectorAll('.settings-bb')).toHaveLength(3)
    expect(document.querySelector('.settings-bb')!.classList.contains('settings-dim')).toBe(true)
    expect(screen.getByText('12')).toBeTruthy()
    expect(screen.getByText('$1.75')).toBeTruthy()
    expect(screen.getByText('20.0%')).toBeTruthy()
    expect(screen.getByText('gui.settings.usage.no_price')).toBeTruthy()
    expect(screen.queryByText('gui.settings.usage.cache_write')).toBeNull()
    expect(screen.getByText('exec').nextElementSibling!.textContent).toBe('2')
  })

  it('shows the cache write column once any model wrote cache', async () => {
    install(undefined, { usage: async () => stats(['2026-09-10'], true) })
    await mount('usage')
    expect(screen.getByText('gui.settings.usage.cache_write')).toBeTruthy()
  })

  it('a custom range is refused before any call when from is after to or outside the 90 days', async () => {
    const { calls } = install(undefined, { usage: async (range) => { calls.push(['usage', range]); return stats([range.from, range.to]) } })
    await mount('usage')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.usage.custom')) })
    const before = calls.length
    const from = screen.getByLabelText('gui.settings.usage.from') as HTMLInputElement
    const to = screen.getByLabelText('gui.settings.usage.to') as HTMLInputElement
    await act(async () => { fireEvent.change(to, { target: { value: '2020-01-01' } }) })
    expect(calls.length).toBe(before)
    expect(toasts.calls).toEqual([`gui.settings.usage.bad_range {"n":${MAX_DAYS}}`])
    const ok = lastDays(3)
    await act(async () => { fireEvent.change(from, { target: { value: ok.from } }) })
    expect(calls[calls.length - 1]).toEqual(['usage', { kind: 'custom', from: ok.from, to: store.get().range.to }])
  })

  it('clampRange keeps only a window inside the last 90 days with from not after to', () => {
    const r = lastDays(MAX_DAYS)
    expect(clampRange(r.from, r.to)).toEqual(r)
    expect(clampRange('2000-01-01', r.to)).toBeNull()
    expect(clampRange(r.to, r.from)).toBeNull()
  })
})
