// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it, vi } from 'vitest'

import type { LaunchResult } from '../lib/externalCli.js'

import { runExternalSetup } from '../app/setupHandoff.js'
import { setupCommands } from '../app/slash/commands/setup.js'

const ctx = () => ({
  gateway: { rpc: vi.fn(async () => ({ provider_configured: true })) },
  session: { newSession: vi.fn() },
  transcript: { sys: vi.fn() }
})

const harness = (launch: LaunchResult) => {
  let insideSuspend = false
  let launchedInsideSuspend = false
  const launcher = vi.fn(async (_args: string[]) => {
    launchedInsideSuspend = insideSuspend

    return launch
  })
  const suspend = vi.fn(async (run: () => Promise<void>) => {
    insideSuspend = true
    try {
      await run()
    } finally {
      insideSuspend = false
    }
  })

  return { launchedInsideSuspend: () => launchedInsideSuspend, launcher, suspend }
}

describe('runExternalSetup', () => {
  it('runs the command inside the Ink suspend and starts a session on success', async () => {
    const c = ctx()
    const h = harness({ code: 0 })

    await runExternalSetup({
      args: ['onboard'],
      ctx: c as never,
      done: 'done',
      launcher: h.launcher,
      suspend: h.suspend
    })

    expect(h.launcher).toHaveBeenCalledWith(['onboard'])
    expect(h.launchedInsideSuspend()).toBe(true)
    expect(c.session.newSession).toHaveBeenCalled()
  })

  it('keeps the session when the command cannot be launched', async () => {
    const c = ctx()
    const h = harness({ code: null, error: 'spawn raven ENOENT' })

    await runExternalSetup({
      args: ['onboard'],
      ctx: c as never,
      done: 'done',
      launcher: h.launcher,
      suspend: h.suspend
    })

    expect(c.session.newSession).not.toHaveBeenCalled()
    expect(c.gateway.rpc).not.toHaveBeenCalled()
  })

  it('keeps the session when the command exits non-zero', async () => {
    const c = ctx()
    const h = harness({ code: 2 })

    await runExternalSetup({
      args: ['onboard'],
      ctx: c as never,
      done: 'done',
      launcher: h.launcher,
      suspend: h.suspend
    })

    expect(c.session.newSession).not.toHaveBeenCalled()
  })
})

describe('/setup', () => {
  it('launches the wizard command the CLI actually registers', () => {
    // `raven setup` is not a registered command, so the previous target could
    // only ever reach the non-zero exit path.
    expect(setupCommands[0]!.help).toContain('raven onboard')
  })
})
