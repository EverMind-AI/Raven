import { renderSync } from '@hermes/ink'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import React from 'react'
import { PassThrough } from 'stream'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { GatewayRpc } from '../app/interfaces.js'

import { getOverlayState, patchOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { getUiState, resetUiState } from '../app/uiStore.js'
import { performDeleteWithFallback, useSessionLifecycle, writeActiveSessionFile } from '../app/useSessionLifecycle.js'
import { hydrateDagRuns, toTranscriptMessages } from '../domain/messages.js'

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

describe('hydrateDagRuns', () => {
  const ROWS = [
    {
      role: 'assistant',
      text: '',
      tool_calls: [{ arguments: '{"nodes":[]}', id: 'call-1', name: 'run_subagent_dag' }]
    },
    { dag_run_id: 'dag-1', name: 'run_subagent_dag', role: 'tool', text: 'DAG run dag-1 finished', tool_call_id: 'call-1' }
  ]

  it('attaches the run its call started', async () => {
    const rpc = vi.fn(async () => ({
      run: {
        dir: '/runs/dag-1',
        files: [{ node: 'a', prompt_template: 'do a', status: 'completed', subagent: 'echo' }],
        finalized: true,
        run_id: 'dag-1',
        summary: { completed: 1, total: 1 }
      }
    }))

    const msgs = await hydrateDagRuns(ROWS, toTranscriptMessages(ROWS), rpc, 'sess')
    const tool = msgs.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!

    expect(tool.dag?.runId).toBe('dag-1')
    expect(tool.dag?.nodes[0]?.promptTemplate).toBe('do a')
    // Must ask quietly: a resumed row losing its graph is not an error the
    // reader has to see, and a non-quiet call would print one on any failure.
    expect(rpc).toHaveBeenCalledWith('dag.get', { run_id: 'dag-1', session_key: 'sess' }, { quiet: true })
  })

  it('leaves the row without a graph when the run dir is gone', async () => {
    // Deleting the outputs must cost the picture, not the transcript.
    const rpc = vi.fn(async () => {
      throw new Error('no such run')
    })

    const msgs = await hydrateDagRuns(ROWS, toTranscriptMessages(ROWS), rpc, 'sess')

    expect(msgs.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!.dag).toBeUndefined()
  })
})


describe('useSessionLifecycle resumeById staleness guard', () => {
  const ROWS_WITH_DAG = [
    {
      role: 'assistant',
      text: '',
      tool_calls: [{ arguments: '{"nodes":[]}', id: 'call-1', name: 'run_subagent_dag' }]
    },
    { dag_run_id: 'dag-a', name: 'run_subagent_dag', role: 'tool', text: 'DAG run dag-a finished', tool_call_id: 'call-1' }
  ]
  const ROWS_PLAIN = [{ role: 'assistant', text: 'hello from b' }]

  const composerActions = () => ({
    clearIn: vi.fn(),
    dequeue: vi.fn(),
    enqueue: vi.fn(),
    handleTextPaste: vi.fn(),
    openEditor: vi.fn(),
    pushHistory: vi.fn(),
    removeQueue: vi.fn(),
    replaceQueue: vi.fn(),
    setCompIdx: vi.fn(),
    setHistoryIdx: vi.fn(),
    setInput: vi.fn(),
    setInputBuf: vi.fn(),
    setPasteSnips: vi.fn(),
    setQueueEdit: vi.fn(),
    syncQueue: vi.fn()
  })

  const deferred = <T,>() => {
    let resolve!: (value: T) => void
    const promise = new Promise<T>(res => {
      resolve = res
    })

    return { promise, resolve }
  }

  const flush = () => new Promise(resolve => setImmediate(resolve))

  type ProbeRpc = (method: string, params?: Record<string, unknown>) => Promise<unknown>

  // Both staleness tests drive the same hook wiring and differ only in which
  // RPC call they gate, so the hook's actions must come from one Probe
  // definition -- a second copy would be a second module-scope ref write for
  // react-compiler/react-compiler to flag.
  const mountLifecycle = (rpc: ProbeRpc, gwRequest: ProbeRpc) => {
    const setHistoryItems = vi.fn()
    const actionsRef: { current: null | ReturnType<typeof useSessionLifecycle> } = { current: null }

    const Probe = () => {
      actionsRef.current = useSessionLifecycle({
        colsRef: { current: 80 },
        composerActions: composerActions() as never,
        gw: { request: gwRequest } as never,
        panel: vi.fn(),
        rpc: rpc as never,
        scrollRef: { current: null },
        setHistoryItems,
        setLastUserMsg: vi.fn(),
        setSessionStartedAt: vi.fn(),
        setVoiceProcessing: vi.fn(),
        setVoiceRecording: vi.fn(),
        sys: vi.fn()
      })

      return null
    }

    const app = renderSync(React.createElement(Probe), { stdout: new PassThrough() as never })

    return { actionsRef, app, setHistoryItems }
  }

  beforeEach(() => {
    resetUiState()
    resetOverlayState()
  })

  it('drops a stale resume once a newer one has already landed', async () => {
    const dagGate = deferred<{ run: Record<string, unknown> }>()

    const rpc = vi.fn(async (method: string) => {
      if (method === 'setup.status') {
        return { provider_configured: true }
      }

      if (method === 'dag.get') {
        return dagGate.promise
      }

      return {}
    })

    const gwRequest = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume' && params?.session_id === 'session-a') {
        return { messages: ROWS_WITH_DAG, session_id: 'session-a' }
      }

      if (method === 'session.resume' && params?.session_id === 'session-b') {
        return { messages: ROWS_PLAIN, session_id: 'session-b' }
      }

      return null
    })

    const { actionsRef, app, setHistoryItems } = mountLifecycle(rpc, gwRequest)

    actionsRef.current!.resumeById('session-a')

    // Let session-a's chain reach its gated dag.get call before session-b starts.
    await vi.waitFor(() =>
      expect(rpc).toHaveBeenCalledWith('dag.get', { run_id: 'dag-a', session_key: 'session-a' }, { quiet: true })
    )

    actionsRef.current!.resumeById('session-b')

    // session-b's transcript carries no DAG call, so hydrateDagRuns resolves
    // without an RPC and this resume can finish first.
    await vi.waitFor(() => expect(getUiState().sid).toBe('session-b'))
    const callsBeforeStaleResolves = setHistoryItems.mock.calls.length

    dagGate.resolve({ run: { dir: '/runs/dag-a', files: [], finalized: true, run_id: 'dag-a' } })
    await flush()
    await flush()

    // Without the epoch guard, session-a's now-unblocked hydrateDagRuns would
    // reach setHistoryItems/patchUiState and overwrite session-b's state.
    expect(setHistoryItems).toHaveBeenCalledTimes(callsBeforeStaleResolves)
    expect(getUiState().sid).toBe('session-b')

    app.unmount()
  })

  it('drops a stale resume whose session.resume answers after a newer one has already landed', async () => {
    const resumeGate = deferred<{ messages: unknown[]; session_id: string }>()

    const rpc = vi.fn(async (method: string) => (method === 'setup.status' ? { provider_configured: true } : {}))

    const gwRequest = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume' && params?.session_id === 'session-a') {
        return resumeGate.promise
      }

      if (method === 'session.resume' && params?.session_id === 'session-b') {
        return { messages: ROWS_PLAIN, session_id: 'session-b' }
      }

      return null
    })

    const { actionsRef, app, setHistoryItems } = mountLifecycle(rpc, gwRequest)

    actionsRef.current!.resumeById('session-a')

    // Let session-a's chain reach its own gated session.resume call -- not
    // dag.get -- before session-b starts, so the epoch it captures is minted
    // before session-b's, matching a resume whose response is merely slow.
    await vi.waitFor(() =>
      expect(gwRequest).toHaveBeenCalledWith('session.resume', { cols: 80, session_id: 'session-a' })
    )

    actionsRef.current!.resumeById('session-b')

    await vi.waitFor(() => expect(getUiState().sid).toBe('session-b'))
    const callsBeforeStaleResolves = setHistoryItems.mock.calls.length

    resumeGate.resolve({ messages: ROWS_WITH_DAG, session_id: 'session-a' })
    await flush()
    await flush()
    await flush()

    // session-a's session.resume answers only after session-b already landed.
    // An epoch captured after session.resume resolves (as opposed to before it
    // is issued) would read the epoch session-b had just minted and pass the
    // check anyway, overwriting session-b's active id and transcript.
    expect(setHistoryItems).toHaveBeenCalledTimes(callsBeforeStaleResolves)
    expect(getUiState().sid).toBe('session-b')

    app.unmount()
  })

  it('drops a stale new session whose session.create answers after a resume has already landed', async () => {
    const createGate = deferred<{ info: null; session_id: string }>()

    const rpc = vi.fn(async (method: string) => {
      if (method === 'setup.status') {
        return { provider_configured: true }
      }

      if (method === 'session.create') {
        return createGate.promise
      }

      return {}
    })

    const gwRequest = vi.fn(async (method: string, params?: Record<string, unknown>) =>
      method === 'session.resume' && params?.session_id === 'session-b'
        ? { messages: ROWS_PLAIN, session_id: 'session-b' }
        : null
    )

    const { actionsRef, app, setHistoryItems } = mountLifecycle(rpc, gwRequest)

    void actionsRef.current!.newSession()

    // Let the new session's chain reach its gated session.create before the
    // resume starts, so its generation is the older of the two.
    await vi.waitFor(() => expect(rpc).toHaveBeenCalledWith('session.create', { cols: 80 }))

    actionsRef.current!.resumeById('session-b')

    await vi.waitFor(() => expect(getUiState().sid).toBe('session-b'))
    const callsBeforeStaleResolves = setHistoryItems.mock.calls.length

    createGate.resolve({ info: null, session_id: 'session-a' })
    await flush()
    await flush()

    // A generation minted only after session.create returned would be bumped
    // here, by this stale call itself, and would then match the ref it had just
    // written -- passing its own staleness check and dragging the visible
    // session back to the one nobody switched to.
    expect(setHistoryItems).toHaveBeenCalledTimes(callsBeforeStaleResolves)
    expect(getUiState().sid).toBe('session-b')

    app.unmount()
  })

  it('does not close the newer session when a stale new session resumes', async () => {
    const setupGate = deferred<{ provider_configured: boolean }>()
    let setupCalls = 0

    const rpc = vi.fn(async (method: string) => {
      if (method === 'setup.status') {
        setupCalls += 1

        return setupCalls === 1 ? setupGate.promise : { provider_configured: true }
      }

      if (method === 'session.create') {
        return { info: null, session_id: 'session-a' }
      }

      return {}
    })

    const gwRequest = vi.fn(async (method: string, params?: Record<string, unknown>) =>
      method === 'session.resume' && params?.session_id === 'session-b'
        ? { messages: ROWS_PLAIN, session_id: 'session-b' }
        : null
    )

    const { actionsRef, app } = mountLifecycle(rpc, gwRequest)

    void actionsRef.current!.newSession()

    await vi.waitFor(() => expect(setupCalls).toBe(1))

    actionsRef.current!.resumeById('session-b')

    await vi.waitFor(() => expect(getUiState().sid).toBe('session-b'))

    setupGate.resolve({ provider_configured: true })
    await flush()
    await flush()

    // newSession picks what to close by reading the live sid, so a stale one
    // resuming here closes the session the newer resume just made active --
    // a backend session torn down under a conversation still on screen.
    expect(rpc).not.toHaveBeenCalledWith('session.close', { session_id: 'session-b' })

    app.unmount()
  })
})
