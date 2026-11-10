// The performance timeline is unbounded and nothing in the TUI reads it back,
// so the monitor's tick clears it. React's development reconciler emitted ~6
// measures per commit; a streaming session accumulated millions of entries and
// the OS killed the process (observed 2026-09-01, heap past 2.5 GB).
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { heapdumpDir } from '../lib/memory.js'
import { startMemoryMonitor } from '../lib/memoryMonitor.js'

describe('startMemoryMonitor', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'] })
  })

  afterEach(() => {
    vi.useRealTimers()
    performance.clearMeasures()
    performance.clearMarks()
  })

  it('clears the performance timeline on every tick, at any pressure level', async () => {
    const stop = startMemoryMonitor({ intervalMs: 1000 })
    for (let i = 0; i < 20; i++) {
      performance.mark(`m${i}`)
      performance.measure(`measure${i}`, `m${i}`)
    }
    expect(performance.getEntriesByType('measure').length).toBe(20)

    await vi.advanceTimersByTimeAsync(1000)

    expect(performance.getEntriesByType('measure').length).toBe(0)
    expect(performance.getEntriesByType('mark').length).toBe(0)
    stop()
  })
})

describe('heapdumpDir', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('follows RAVEN_HOME so a second install keeps its own heap dumps', () => {
    const home = join(tmpdir(), 'raven-secondary')
    vi.stubEnv('RAVEN_HOME', home)
    vi.stubEnv('RAVEN_HEAPDUMP_DIR', '')

    expect(heapdumpDir()).toBe(join(home, 'heapdumps'))
  })

  it('keeps RAVEN_HEAPDUMP_DIR as an explicit override', () => {
    vi.stubEnv('RAVEN_HOME', join(tmpdir(), 'raven-secondary'))
    vi.stubEnv('RAVEN_HEAPDUMP_DIR', join(tmpdir(), 'raven-dumps'))

    expect(heapdumpDir()).toBe(join(tmpdir(), 'raven-dumps'))
  })
})
