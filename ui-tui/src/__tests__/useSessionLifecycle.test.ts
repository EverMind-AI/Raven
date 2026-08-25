import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { GatewayRpc } from '../app/interfaces.js'

import { getOverlayState, patchOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { performDeleteWithFallback, writeActiveSessionFile } from '../app/useSessionLifecycle.js'

describe('writeActiveSessionFile', () => {
  let dir = ''

  afterEach(() => {
    if (dir) {
      rmSync(dir, { force: true, recursive: true })
      dir = ''
    }
  })

  it('writes the actual resumed session id for the shell exit summary', () => {
    dir = mkdtempSync(join(tmpdir(), 'raven-tui-active-'))
    const path = join(dir, 'active.json')

    writeActiveSessionFile('actual_session', path)

    expect(JSON.parse(readFileSync(path, 'utf8'))).toEqual({ session_id: 'actual_session' })
  })
})

describe('performDeleteWithFallback', () => {
  beforeEach(() => {
    resetOverlayState()
  })

  const makeDeps = (
    mostRecent: { session_id?: null | string } | null = null,
    activeSid: null | string = 'tui:active'
  ) => {
    const calls: { method: string; params: unknown }[] = []

    const rpc = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      if (method === 'session.most_recent') {
        return mostRecent
      }

      if (method === 'session.delete') {
        return { deleted: params?.session_id }
      }

      return {}
    })

    return {
      calls,
      deps: {
        activeSid,
        newSession: vi.fn(async () => {}),
        resumeById: vi.fn(),
        rpc: rpc as unknown as GatewayRpc
      }
    }
  }

  it('non-active delete: deletes and stops (no fresh session)', async () => {
    const { calls, deps } = makeDeps()

    await performDeleteWithFallback('tui:other', deps)

    expect(calls.map(c => c.method)).toEqual(['session.delete'])
    expect(deps.resumeById).not.toHaveBeenCalled()
    expect(deps.newSession).not.toHaveBeenCalled()
  })

  it('active delete always mints a fresh session, even when a survivor exists', async () => {
    const { calls, deps } = makeDeps({ session_id: 'tui:survivor' })

    await performDeleteWithFallback('tui:active', deps)

    expect(calls.map(c => c.method)).toEqual(['session.delete'])
    expect(deps.resumeById).not.toHaveBeenCalled()
    expect(deps.newSession).toHaveBeenCalledTimes(1)
  })

  it('closes the picker overlay before minting the fresh session', async () => {
    patchOverlayState({ picker: true })
    const { deps } = makeDeps()

    await performDeleteWithFallback('tui:active', deps)

    expect(getOverlayState().picker).toBe(false)
    expect(deps.newSession).toHaveBeenCalledTimes(1)
  })

  it('resolves true when the server confirms the removal', async () => {
    const { deps } = makeDeps()

    await expect(performDeleteWithFallback('tui:other', deps)).resolves.toBe(true)
  })

  it('resolves false when the server returns deleted: null (no such session)', async () => {
    const { deps } = makeDeps()
    const rpc = vi.fn(async (method: string) => (method === 'session.delete' ? { deleted: null } : {}))
    deps.rpc = rpc as unknown as GatewayRpc

    await expect(performDeleteWithFallback('tui:other', deps)).resolves.toBe(false)
  })
})
