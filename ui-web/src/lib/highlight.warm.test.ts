// @vitest-environment happy-dom
/* The highlighter's warm-up: building it and a grammar's first read are the
 * expensive part of colouring a listing, so the page does both in the
 * background after boot, a piece per task, and the first listing that closes
 * later is coloured warm. */

import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HighlighterCore } from 'shiki/core'
import type { Mock } from 'vitest'

afterEach(() => {
  vi.doUnmock('shiki/core')
  vi.unstubAllGlobals()
  vi.useRealTimers()
  vi.resetModules()
})

/* A fresh highlight module, over a Shiki whose constructor is counted and
   whose highlighter records every grammar it is asked to read. */
async function fresh(): Promise<{ mod: typeof import('./highlight'); made: Mock; reads: string[] }> {
  vi.resetModules()
  const reads: string[] = []
  vi.doMock('shiki/core', async (importOriginal) => {
    const real = await importOriginal<typeof import('shiki/core')>()
    const createHighlighterCoreSync = vi.fn((...args: Parameters<typeof real.createHighlighterCoreSync>) => {
      const hl: HighlighterCore = real.createHighlighterCoreSync(...args)
      const read = hl.codeToTokensBase.bind(hl)
      hl.codeToTokensBase = (code, options) => {
        reads.push(String(options.lang))
        return read(code, options)
      }
      return hl
    })
    return { ...real, createHighlighterCoreSync }
  })
  const mod = await import('./highlight')
  const made = (await import('shiki/core')).createHighlighterCoreSync as unknown as Mock
  return { made, mod, reads }
}

/* A scheduler that holds every task until the test runs it. */
function held(): { later: (task: () => void) => void; tasks: Array<() => void> } {
  const tasks: Array<() => void> = []
  return { later: (task) => { tasks.push(task) }, tasks }
}

describe('warming the highlighter', () => {
  it('does nothing until the scheduler runs it', async () => {
    const { made, mod } = await fresh()
    const q = held()
    mod.warm(q.later)
    expect(made).not.toHaveBeenCalled()
    expect(q.tasks).toHaveLength(1)
  })

  it('builds the highlighter in one task, then reads each grammar once, one per task', async () => {
    const { made, mod, reads } = await fresh()
    const q = held()
    mod.warm(q.later)
    q.tasks.shift()!()
    expect(made).toHaveBeenCalledTimes(1)
    expect(reads).toHaveLength(0)
    while (q.tasks.length) {
      const before = reads.length
      q.tasks.shift()!()
      expect(reads.length - before).toBe(1)
    }
    /* Every name the highlighter answers to is a grammar that was read, and
       none was read twice: an alias (ts, py, sh) shares its grammar. */
    const hl = made.mock.results[0]!.value as HighlighterCore
    const warmed = new Set(reads.map((name) => hl.getLanguage(name)))
    expect(warmed.size).toBe(reads.length)
    const cold = hl.getLoadedLanguages().filter((name) => !warmed.has(hl.getLanguage(name)))
    expect(cold).toEqual([])
  })

  it('warms once, however often it is asked', async () => {
    const { mod } = await fresh()
    const q = held()
    mod.warm(q.later)
    mod.warm(q.later)
    expect(q.tasks).toHaveLength(1)
  })

  it('leaves the highlighter it built to the listings that come after', async () => {
    const { made, mod } = await fresh()
    const q = held()
    mod.warm(q.later)
    while (q.tasks.length) q.tasks.shift()!()
    expect(mod.highlight('const a = 1', 'ts')).toContain('var(--code-')
    expect(made).toHaveBeenCalledTimes(1)
  })
})

describe('the warm-up scheduler', () => {
  it('asks the platform scheduler for background priority where there is one', async () => {
    const postTask = vi.fn(() => Promise.resolve())
    vi.stubGlobal('scheduler', { postTask })
    const { made, mod } = await fresh()
    mod.warm()
    expect(postTask).toHaveBeenCalledWith(expect.any(Function), { priority: 'background' })
    expect(made).not.toHaveBeenCalled()
  })

  it('falls back to an idle callback', async () => {
    const requestIdleCallback = vi.fn()
    vi.stubGlobal('requestIdleCallback', requestIdleCallback)
    const { mod } = await fresh()
    mod.warm()
    expect(requestIdleCallback).toHaveBeenCalledWith(expect.any(Function))
  })

  it('falls back to a timer where the page has neither', async () => {
    vi.useFakeTimers()
    const { made, mod } = await fresh()
    mod.warm()
    expect(made).not.toHaveBeenCalled()
    vi.advanceTimersToNextTimer()
    expect(made).toHaveBeenCalledTimes(1)
  })
})
