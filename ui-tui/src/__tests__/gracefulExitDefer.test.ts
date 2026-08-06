// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it, vi } from 'vitest'

import { deferSignalExit, setupGracefulExit } from '../lib/gracefulExit.js'

describe('deferSignalExit', () => {
  it('keeps a signal from ending the session while a child owns the terminal', async () => {
    // The child is in this process group and receives the same Ctrl-C. Exiting
    // here would take the session down with the sign-in the user interrupted.
    const cleanup = vi.fn()
    const exit = vi.spyOn(process, 'exit').mockImplementation(() => undefined as never)
    setupGracefulExit({ cleanups: [cleanup], failsafeMs: 5 })

    const restore = deferSignalExit()
    process.emit('SIGINT')
    await new Promise(resolve => setTimeout(resolve, 20))

    expect(cleanup).not.toHaveBeenCalled()
    expect(exit).not.toHaveBeenCalled()

    restore()
    process.emit('SIGINT')
    await new Promise(resolve => setTimeout(resolve, 20))

    expect(cleanup).toHaveBeenCalled()

    exit.mockRestore()
  })

  it('releases once, so nested handoffs cannot leave signals deferred', async () => {
    // A fresh module: the deferral count and the signal wiring are module state,
    // and the test above has already wired this file's copy.
    vi.resetModules()
    const fresh = await import('../lib/gracefulExit.js')

    const cleanup = vi.fn()
    const exit = vi.spyOn(process, 'exit').mockImplementation(() => undefined as never)
    fresh.setupGracefulExit({ cleanups: [cleanup], failsafeMs: 5 })

    const outer = fresh.deferSignalExit()
    const inner = fresh.deferSignalExit()

    // Released twice by the handoff that owns it. The second call must not
    // decrement the count a still-running handoff is holding.
    inner()
    inner()

    process.emit('SIGINT')
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(cleanup).not.toHaveBeenCalled()

    outer()
    process.emit('SIGINT')
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(cleanup).toHaveBeenCalled()

    exit.mockRestore()
  })
})
