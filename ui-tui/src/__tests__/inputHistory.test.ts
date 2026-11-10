// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// R14: the composer's up/down history cycling (cycleHistory in
// useInputHandlers.ts) is a closure inside the hook and not exported, so it
// walks the backing store returned by useInputHistory -> lib/history.ts. These
// tests pin that store's contract: insertion order (oldest first, newest last),
// consecutive dedup, and the on-disk `+`-prefixed round-trip that load() parses
// back. cycleHistory relies on this ordering: dir<0 starts at the last index
// (newest) and walks toward index 0 (oldest); dir>0 walks forward again.

import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

import type { append, load } from '../lib/history.js'

interface HistoryModule {
  append: typeof append
  load: typeof load
}

let history: HistoryModule

beforeAll(async () => {
  const dir = mkdtempSync(join(tmpdir(), 'raven-hist-'))

  vi.stubEnv('RAVEN_HOME', dir)
  history = await import('../lib/history.js')
})

afterAll(() => {
  vi.unstubAllEnvs()
})

describe('input history store (backs cycleHistory)', () => {
  it('starts empty when no history file exists', () => {
    expect(history.load()).toEqual([])
  })

  it('appends entries in insertion order with newest last', () => {
    history.append('first command')
    history.append('second command')
    history.append('third command')

    const entries = history.load()

    expect(entries).toEqual(['first command', 'second command', 'third command'])
    // cycle-up (dir<0) surfaces the newest first...
    expect(entries.at(-1)).toBe('third command')
    // ...and walks down to the oldest at index 0.
    expect(entries[0]).toBe('first command')
  })

  it('deduplicates a repeat of the most recent entry', () => {
    history.append('third command')

    expect(history.load()).toEqual(['first command', 'second command', 'third command'])
  })

  it('trims leading/trailing whitespace and ignores blank input', () => {
    history.append('   ')
    history.append('  spaced command  ')

    const entries = history.load()

    expect(entries.at(-1)).toBe('spaced command')
    expect(entries).not.toContain('   ')
  })

  it('round-trips a multi-line entry through the on-disk format', async () => {
    history.append('line a\nline b')

    vi.resetModules()
    const reloaded: HistoryModule = await import('../lib/history.js')

    expect(reloaded.load().at(-1)).toBe('line a\nline b')
    expect(reloaded.load()).toEqual([
      'first command',
      'second command',
      'third command',
      'spaced command',
      'line a\nline b'
    ])
  })

  it('loads a CRLF file with the same entries an LF one yields', async () => {
    const crlfDir = mkdtempSync(join(tmpdir(), 'raven-hist-crlf-'))
    const suiteHome = process.env.RAVEN_HOME

    vi.stubEnv('RAVEN_HOME', crlfDir)
    try {
      writeFileSync(
        join(crlfDir, '.raven_history'),
        '# 2026-11-09 00:00:00\r\n+first command\r\n# 2026-11-09 00:00:01\r\n+second command\r\n# 2026-11-09 00:00:02\r\n+line a\r\n+line b\r\n'
      )

      vi.resetModules()
      const reloaded: HistoryModule = await import('../lib/history.js')

      expect(reloaded.load()).toEqual(['first command', 'second command', 'line a\nline b'])
    } finally {
      vi.stubEnv('RAVEN_HOME', suiteHome)
    }
  })

  it('loads a file of lone CRs with the same entries an LF one yields', async () => {
    // `Path.read_text` applies universal-newline translation, so the host
    // reads a CR-only file the way it reads an LF one. The TUI has to fold
    // the same endings, or the two front ends disagree about the file they
    // share.
    const crDir = mkdtempSync(join(tmpdir(), 'raven-hist-cr-'))
    const suiteHome = process.env.RAVEN_HOME

    vi.stubEnv('RAVEN_HOME', crDir)
    try {
      writeFileSync(
        join(crDir, '.raven_history'),
        '# 2026-11-09 00:00:00\r+first command\r# 2026-11-09 00:00:01\r+second command\r'
      )

      vi.resetModules()
      const reloaded: HistoryModule = await import('../lib/history.js')

      expect(reloaded.load()).toEqual(['first command', 'second command'])
    } finally {
      vi.stubEnv('RAVEN_HOME', suiteHome)
    }
  })
})
