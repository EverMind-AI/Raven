// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { homedir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ravenHomeDir } from '../lib/ravenHome.js'

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('ravenHomeDir (the TUI reader of RAVEN_HOME)', () => {
  it('defaults to ~/.raven when RAVEN_HOME is unset', () => {
    vi.stubEnv('RAVEN_HOME', undefined)

    expect(ravenHomeDir()).toBe(join(homedir(), '.raven'))
  })

  it('treats a blank RAVEN_HOME as unset', () => {
    vi.stubEnv('RAVEN_HOME', '   ')

    expect(ravenHomeDir()).toBe(join(homedir(), '.raven'))
  })

  it('keeps an absolute value', () => {
    vi.stubEnv('RAVEN_HOME', '/srv/raven')

    expect(ravenHomeDir()).toBe('/srv/raven')
  })

  it('expands a leading ~', () => {
    vi.stubEnv('RAVEN_HOME', '~/state')

    expect(ravenHomeDir()).toBe(join(homedir(), 'state'))
  })

  it('expands a leading ~\\ (the Windows spelling)', () => {
    vi.stubEnv('RAVEN_HOME', '~\\state')

    expect(ravenHomeDir()).toBe(join(homedir(), 'state'))
  })

  it('expands a bare ~', () => {
    vi.stubEnv('RAVEN_HOME', '~')

    expect(ravenHomeDir()).toBe(homedir())
  })

  it('keeps a relative value relative, like the host does', () => {
    vi.stubEnv('RAVEN_HOME', 'state')

    expect(ravenHomeDir()).toBe('state')
  })

  it('keeps a ~name value relative (the host expands it, the TUI does not)', () => {
    vi.stubEnv('RAVEN_HOME', '~bob/state')

    expect(ravenHomeDir()).toBe('~bob/state')
  })
})
