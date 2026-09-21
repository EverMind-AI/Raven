// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { resetSources, setSources } from '../../state/sources'
import { ImportSyncApp } from './ImportSyncPage'
import * as store from './store'

import type { ImportStatus, ImportSyncSource, ImportTier } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const status = (patch: Partial<ImportStatus> = {}): ImportStatus => ({
  running: false, total: 0, submitted: 0, failed: 0, by_platform: {}, phase: null, phases: null, tier: null, platforms: [], ...patch,
})

const runs: [string[], ImportTier][] = []
let stops = 0
const src: ImportSyncSource = {
  status: async () => store.get().status ?? status(),
  run: async (platforms, tier) => { runs.push([platforms, tier]); return { started: true, total: 1, detail: '' } },
  stop: async () => { stops += 1; return { stopped: true } },
}

beforeEach(() => {
  setTranslator((key, vars) => (vars ? `${key}:${JSON.stringify(vars)}` : key))
  setSources({ importSync: src })
  runs.length = 0
  stops = 0
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  resetSources()
  resetTranslator()
  localStorage.clear()
})

const draw = (st: ImportStatus | null): void => {
  store.set({ status: st })
  render(<ImportSyncApp />)
}
const row = (): HTMLElement | null => document.querySelector<HTMLElement>('.importSync')
const main = (): HTMLButtonElement => document.querySelector<HTMLButtonElement>('.importSync-main')!
const x = (): HTMLButtonElement | null => document.querySelector<HTMLButtonElement>('.importSync-x')
const settle = async (): Promise<void> => { await act(async () => { await Promise.resolve(); await Promise.resolve() }) }

describe('the import row', () => {
  it('draws nothing with no run on disk', () => {
    draw(null)
    expect(row()).toBeNull()
  })

  it('names the message pass with its settled share and offers a stop', () => {
    draw(status({ running: true, total: 18, submitted: 6, failed: 1 }))
    expect(row()?.className).toContain('importSync-run')
    expect(row()?.textContent).toContain('gui.importSync.run')
    expect(row()?.textContent).toContain('39%')
    expect(main().disabled).toBe(true)
    expect(x()?.getAttribute('aria-label')).toBe('gui.importSync.stop')
    expect(row()?.querySelector<HTMLElement>('.importSync-bar i')?.style.width).toBe('39%')
  })

  it("names the phase in flight with the phase's own count", () => {
    draw(status({ running: true, total: 18, submitted: 18, phase: { kind: 'skills', current: 1, total: 2 } }))
    expect(row()?.className).toContain('importSync-wrap')
    expect(row()?.textContent).toContain('gui.importSync.skills')
    expect(row()?.textContent).toContain('1/2')
  })

  it('offers to resume a run the gateway lost, with the request it recorded', async () => {
    draw(status({ total: 18, submitted: 11, tier: 'memory_files', platforms: ['claude_code'] }))
    expect(row()?.className).toContain('importSync-paused')
    expect(row()?.textContent).toContain('gui.importSync.paused')
    expect(main().disabled).toBe(false)
    expect(main().getAttribute('aria-label')).toBe('gui.importSync.resume')
    expect(x()).toBeNull()

    await act(async () => { fireEvent.click(main()) })
    await settle()

    expect(runs).toEqual([[['claude_code'], 'memory_files']])
  })

  it('says how many did not make it and offers a retry, with nothing to dismiss', () => {
    draw(status({ total: 18, submitted: 15, failed: 3, tier: 'full', platforms: ['claude_code'], phases: { status: 'done', errors: [] } }))
    expect(row()?.className).toContain('importSync-done')
    expect(row()?.className).toContain('importSync-warn')
    expect(row()?.textContent).toContain('gui.importSync.failed_n:{"n":3}')
    expect(main().disabled).toBe(false)
    expect(main().getAttribute('aria-label')).toBe('gui.importSync.retry')
    expect(x()).toBeNull()
  })

  it('shows a stopped run it cannot ask for again as paused with nothing to click', () => {
    draw(status({ total: 18, submitted: 11 }))
    expect(row()?.className).toContain('importSync-paused')
    expect(main().disabled).toBe(true)
    expect(main().getAttribute('aria-label')).toBeNull()
    expect(x()).toBeNull()
  })

  it('counts a failed phase among what did not make it', () => {
    draw(status({ total: 2, submitted: 2, tier: 'full', platforms: ['hermes'], phases: { status: 'failed', errors: ['profile: bad byte'] } }))
    expect(row()?.textContent).toContain('gui.importSync.failed_n:{"n":1}')
    expect(main().getAttribute('aria-label')).toBe('gui.importSync.retry')
  })

  it('a clean finish carries a dismiss that takes the row down', async () => {
    draw(status({ total: 4, submitted: 4, tier: 'full', platforms: ['hermes'], phases: { status: 'done', errors: [] } }))
    expect(row()?.textContent).toContain('gui.importSync.done')
    expect(x()?.getAttribute('aria-label')).toBe('gui.importSync.dismiss')

    await act(async () => { fireEvent.click(x()!) })

    expect(row()).toBeNull()
  })

  it('the stop asks the gateway', async () => {
    draw(status({ running: true, total: 4, submitted: 1 }))

    await act(async () => { fireEvent.click(x()!) })
    await settle()

    expect(stops).toBe(1)
  })
})
