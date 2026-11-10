// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The TUI's half of the RAVEN_HOME contract: one resolver, so the composer
// history, the loop-lag log and the perf pane name one directory -- and the
// same one the Python side names (`raven_home()` in raven/home.py).

import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { ravenHome } from '../lib/ravenHome.js'

const original = process.env.RAVEN_HOME

afterEach(() => {
  if (original === undefined) {
    delete process.env.RAVEN_HOME
  } else {
    process.env.RAVEN_HOME = original
  }
})

describe('ravenHome', () => {
  it('falls back to the default home when the variable is unset', () => {
    delete process.env.RAVEN_HOME

    expect(ravenHome()).toBe(join(homedir(), '.raven'))
  })

  it('reads a blank value as unset, not as the working directory', () => {
    // `RAVEN_HOME=` in a shell profile names no home. The TUI used to join
    // the empty string and read the history from the working directory while
    // the Python side fell back to `~/.raven`.
    process.env.RAVEN_HOME = '   '

    expect(ravenHome()).toBe(join(homedir(), '.raven'))
  })

  it('expands a tilde the way the Python side expands it', () => {
    process.env.RAVEN_HOME = '~/raven-two'
    expect(ravenHome()).toBe(join(homedir(), 'raven-two'))

    process.env.RAVEN_HOME = '~'
    expect(ravenHome()).toBe(homedir())
  })

  it('keeps an absolute path as it is', () => {
    process.env.RAVEN_HOME = '/srv/raven-home'

    expect(ravenHome()).toBe('/srv/raven-home')
  })

  it('resolves a relative value against the working directory', () => {
    // Python leaves a relative RAVEN_HOME relative and reads it against the
    // working directory; resolving it here names that same file.
    process.env.RAVEN_HOME = 'rel-home'

    expect(ravenHome()).toBe(resolve('rel-home'))
  })
})
