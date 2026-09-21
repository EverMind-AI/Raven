// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../state/sources'
import * as store from './store'

import type { ImportStatus, ImportSyncSource, ImportTier } from './types'

const status = (patch: Partial<ImportStatus> = {}): ImportStatus => ({
  running: false, total: 0, submitted: 0, failed: 0, by_platform: {}, phase: null, phases: null, tier: null, platforms: [], ...patch,
})

const state = (st: ImportStatus | null, patch: Partial<store.ImportSyncState> = {}): store.ImportSyncState => ({
  status: st, starting: false, dismissed: '', error: '', ...patch,
})

interface Fake {
  src: ImportSyncSource
  runs: [string[], ImportTier][]
  stops: number
  reads: number
}

/* A gateway whose status answers come from a script: each read pops the next
   answer and the last one repeats. */
const fake = (answers: ImportStatus[]): Fake => {
  const f: Fake = { runs: [], stops: 0, reads: 0, src: null as unknown as ImportSyncSource }
  const queue = [...answers]
  f.src = {
    status: async () => { f.reads += 1; return (queue.length > 1 ? queue.shift() : queue[0]) as ImportStatus },
    run: async (platforms, tier) => { f.runs.push([platforms, tier]); return { started: true, total: 1, detail: '' } },
    stop: async () => { f.stops += 1; return { stopped: true } },
  }
  return f
}

afterEach(() => {
  store._resetForTests()
  resetSources()
  vi.useRealTimers()
  localStorage.clear()
})

describe('what the row shows', () => {
  it('nothing, with no run on disk', () => {
    expect(store.view(state(null)).kind).toBe('hidden')
    expect(store.view(state(status())).kind).toBe('hidden')
  })

  it('a scan while a start is in flight and nothing runs yet', () => {
    expect(store.view(state(null, { starting: true })).kind).toBe('scan')
    expect(store.view(state(status({ running: true, total: 3 }), { starting: true })).kind).toBe('run')
  })

  it('the settled share while the message pass is on, failures counted as settled', () => {
    const v = store.view(state(status({ running: true, total: 18, submitted: 6, failed: 1 })))
    expect(v).toMatchObject({ kind: 'run', pct: 39, failed: 1, clickable: false })
  })

  it('the settled share moves with the source the pass is on', () => {
    /* 18 sources, 7 settled, the eighth 40 of 287 messages in: 7.14 of 18. */
    const v = store.view(state(status({
      running: true, total: 18, submitted: 6, failed: 1,
      current: { platform: 'claude_code', source_key: 'a', sent: 40, total: 287 },
    })))
    expect(v).toMatchObject({ kind: 'run', pct: 40 })
    expect(store.view(state(status({ running: true, total: 18, submitted: 6, failed: 1 }))).pct).toBe(39)
  })

  it('counts no share for a source with nothing to count, or one that reports more than it holds', () => {
    const on = { running: true, total: 18, submitted: 6, failed: 1 }
    const src = { platform: 'claude_code', source_key: 'a' }
    expect(store.view(state(status({ ...on, current: { ...src, sent: 0, total: 0 } }))).pct).toBe(39)
    expect(store.view(state(status({ ...on, current: { ...src, sent: 300, total: 287 } }))).pct).toBe(44)
  })

  it('the share is the fraction of the source that landed, batch by batch', () => {
    /* One source of 200 messages is the whole bar: each batch of 10 is 5 points. */
    const pct = (sent: number): number => store.view(state(status({
      running: true, total: 1, submitted: 0, failed: 0,
      current: { platform: 'claude_code', source_key: 'a', sent, total: 200 },
    }))).pct
    expect([pct(0), pct(10), pct(100), pct(190)]).toEqual([0, 5, 50, 95])
  })

  it('stops short of 100 while a source is still being sent', () => {
    /* 17 of 18 settled and the last one nine minutes from done rounds to 100:
       the 100-percent-then-wait a progress row exists to remove. */
    const v = store.view(state(status({
      running: true, total: 18, submitted: 17, failed: 0,
      current: { platform: 'claude_code', source_key: 'r', sent: 2730, total: 3000 },
    })))
    expect(v).toMatchObject({ kind: 'run', pct: 99 })
    expect(store.view(state(status({ total: 4, submitted: 4, tier: 'full', platforms: ['hermes'], phases: { status: 'done', errors: [] } }))).pct).toBe(100)
  })

  it('counts no share for the source a run that is no longer on was left pointing at', () => {
    /* Nothing is being fed, so the settled counts are the whole truth: a share
       added on top of them would draw the same source twice. */
    const v = store.view(state(status({
      running: false, total: 18, submitted: 11, failed: 1, tier: 'memory_files', platforms: ['claude_code'],
      current: { platform: 'claude_code', source_key: 'a', sent: 200, total: 287 },
    })))
    expect(v).toMatchObject({ kind: 'paused', pct: 67 })
  })

  it("a phase's own share while one is on", () => {
    const v = store.view(state(status({ running: true, total: 18, submitted: 18, phase: { kind: 'profile', current: 1, total: 3 } })))
    expect(v).toMatchObject({ kind: 'wrap', pct: 33, phase: { kind: 'profile', current: 1, total: 3 } })
  })

  it('paused when nothing runs and the counts fall short of the total -- the gateway lost the run', () => {
    const v = store.view(state(status({ running: false, total: 18, submitted: 11, failed: 1, tier: 'full', platforms: ['hermes'] })))
    expect(v).toMatchObject({ kind: 'paused', pct: 67, clickable: true })
  })

  it('done, and clickable exactly when something failed and the request is on file', () => {
    const asked = { tier: 'full' as const, platforms: ['hermes'], phases: { status: 'done' as const, errors: [] } }
    expect(store.view(state(status({ total: 4, submitted: 4, ...asked })))).toMatchObject({ kind: 'done', pct: 100, failed: 0, clickable: false })
    expect(store.view(state(status({ total: 4, submitted: 3, failed: 1, ...asked })))).toMatchObject({ kind: 'done', failed: 1, clickable: true })
    expect(store.view(state(status({ total: 4, submitted: 3, failed: 1 })))).toMatchObject({ kind: 'done', failed: 1, clickable: false })
  })

  it('paused, but not clickable, when the file does not say what was asked', () => {
    /* A run the CLI started records no tier; a guess ("full") could turn a
       minutes-long import into hours, so the row shows it and offers nothing. */
    expect(store.view(state(status({ total: 4, submitted: 2 })))).toMatchObject({ kind: 'paused', clickable: false })
  })

  it('paused while the phases behind a settled pass are pending, cancelled, or never recorded for a run that asked', () => {
    const settled = status({ total: 2, submitted: 2, tier: 'memory_files', platforms: ['claude_code'] })
    expect(store.view(state({ ...settled, phases: { status: 'pending', errors: [] } }))).toMatchObject({ kind: 'paused', pct: 100, clickable: true })
    expect(store.view(state({ ...settled, phases: { status: 'cancelled', errors: [] } })).kind).toBe('paused')
    expect(store.view(state(settled)).kind).toBe('paused')
    expect(store.view(state({ ...settled, phases: { status: 'done', errors: [] } })).kind).toBe('done')
  })

  it('a run with no request and no phase verdict is simply finished', () => {
    expect(store.view(state(status({ total: 2, submitted: 2 }))).kind).toBe('done')
  })

  it('a failed phase is something that did not make it, and can be retried', () => {
    const v = store.view(state(status({
      total: 2, submitted: 2, tier: 'full', platforms: ['hermes'], phases: { status: 'failed', errors: ['profile: bad byte'] },
    })))
    expect(v).toMatchObject({ kind: 'done', failed: 1, clickable: true })
  })

  it('a skills-only run has no counts and still shows its phases', () => {
    const only = status({ total: 0, tier: 'memory_files', platforms: ['claude_code'] })
    expect(store.view(state({ ...only, phases: { status: 'pending', errors: [] } })).kind).toBe('paused')
    expect(store.view(state({ ...only, phases: { status: 'done', errors: [] } })).kind).toBe('done')
  })

  it('hidden again once that finished run was dismissed, but not for a different one', () => {
    const finished = status({
      total: 4, submitted: 4, tier: 'memory_files', platforms: ['claude_code'], phases: { status: 'done', errors: [] },
    })
    const dismissed = store.signature(finished)
    expect(store.view(state(finished, { dismissed })).kind).toBe('hidden')
    expect(store.view(state({ ...finished, total: 5, submitted: 5 }, { dismissed })).kind).toBe('done')
  })
})

describe('following a run', () => {
  beforeEach(() => { vi.useFakeTimers() })

  it('polls while the gateway says a run is on, and stops when it says it ended', async () => {
    const f = fake([
      status({ running: true, total: 2, submitted: 0 }),
      status({ running: true, total: 2, submitted: 1 }),
      status({ running: false, total: 2, submitted: 2 }),
    ])
    setSources({ importSync: f.src })

    await store.refresh()
    expect(f.reads).toBe(1)
    await vi.advanceTimersByTimeAsync(store.POLL_MS)
    expect(f.reads).toBe(2)
    await vi.advanceTimersByTimeAsync(store.POLL_MS)
    expect(f.reads).toBe(3)
    expect(store.get().status?.running).toBe(false)
    await vi.advanceTimersByTimeAsync(store.POLL_MS * 3)
    expect(f.reads).toBe(3)
  })

  it('resume asks for the run the status recorded, then follows it', async () => {
    const f = fake([status({ running: false, total: 18, submitted: 11, tier: 'memory_files', platforms: ['claude_code', 'hermes'] })])
    setSources({ importSync: f.src })
    await store.refresh()

    await store.resume()

    expect(f.runs).toEqual([[['claude_code', 'hermes'], 'memory_files']])
    expect(store.get().starting).toBe(false)
  })

  it('resume does nothing when the file does not say what was asked', async () => {
    const f = fake([status({ running: false, total: 18, submitted: 11 })])
    setSources({ importSync: f.src })
    await store.refresh()

    await store.resume()

    expect(f.runs).toEqual([])
  })

  it('resume does nothing while a run is on', async () => {
    const f = fake([status({ running: true, total: 2, submitted: 1, platforms: ['hermes'] })])
    setSources({ importSync: f.src })
    await store.refresh()

    await store.resume()

    expect(f.runs).toEqual([])
  })

  it('a start that the gateway refuses is reported, not swallowed', async () => {
    const f = fake([status()])
    f.src.run = async () => ({ started: false, total: 0, detail: 'an import is already running' })
    setSources({ importSync: f.src })

    const r = await store.start(['hermes'], 'full')

    expect(r.started).toBe(false)
    expect(store.get().error).toBe('an import is already running')
  })

  it('stop tells the gateway and re-reads', async () => {
    const f = fake([status({ running: true, total: 2, submitted: 1 }), status({ running: false, total: 2, submitted: 1 })])
    setSources({ importSync: f.src })
    await store.refresh()

    await store.stop()

    expect(f.stops).toBe(1)
    expect(store.view(store.get()).kind).toBe('paused')
  })

  it('dismiss remembers the finished run across a reload', async () => {
    const finished = status({ total: 4, submitted: 4, tier: 'full', platforms: ['hermes'], phases: { status: 'done', errors: [] } })
    const f = fake([finished])
    setSources({ importSync: f.src })
    await store.refresh()
    expect(store.view(store.get()).kind).toBe('done')

    store.dismiss()

    expect(store.view(store.get()).kind).toBe('hidden')
    expect(localStorage.getItem('raven.importSync.dismissed')).toBe(store.signature(finished))
  })
})
