// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The composer history file, read the way the Python reader reads it.
//
// `Path.read_text` applies universal-newline translation, so a file an
// external editor converted to CRLF gives clean entries on the Python side.
// The TUI split on '\n' and kept the trailing '\r', which rode into the entry
// and defeated the adjacent-duplicate check. Both readers now fold the same
// endings, so the two front ends read one file the same way.

import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

let dir: string

beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), 'raven-hist-eol-'))
  vi.stubEnv('RAVEN_HOME', dir)
})

afterAll(() => {
  vi.unstubAllEnvs()
})

/** A fresh reader over the file as it stands now: the module caches its load. */
async function reread(): Promise<string[]> {
  vi.resetModules()
  const history = await import('../lib/history.js')

  return history.load()
}

describe('the history reader and line endings', () => {
  it('reads a CRLF file the way the Python reader reads it', async () => {
    writeFileSync(join(dir, '.raven_history'), '# 2026-11-10 09:00:00.000\r\n+first\r\n+second\r\n')

    expect(await reread()).toEqual(['first\nsecond'])
  })

  it('folds a lone CR the way the Python reader does', async () => {
    writeFileSync(join(dir, '.raven_history'), '+first\r+second\r')

    expect(await reread()).toEqual(['first\nsecond'])
  })

  it('still reads an LF file the same way', async () => {
    writeFileSync(join(dir, '.raven_history'), '# 2026-11-10 09:00:00.000\n+first\n+second\n')

    expect(await reread()).toEqual(['first\nsecond'])
  })
})
