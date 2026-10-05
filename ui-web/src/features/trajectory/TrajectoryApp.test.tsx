// @vitest-environment happy-dom
import { act, cleanup, render } from '@testing-library/react'
// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { absorb, resetCapabilities } from '../../rpc/capabilities'
import { RpcError } from '../../rpc/transport'
import { _resetFreshForTests, unpitch } from '../../state/session/conversation'
import { resetSources, setSources } from '../../state/sources'
import * as store from './store'
import { TrajectoryApp } from './TrajectoryApp'

import type { TrajectoryEntry, TrajectoryIndexState, TrajectorySource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const READY: TrajectoryIndexState = {
  phase: 'ready', scanned_bytes: 10, total_bytes: 10, head_truncated: 0, recovering_traces: 0,
  unresolved_traces: 0, unresolved_dropped: 0, oversized_lines_dropped: 0, preview_pending: 0, failure: null,
}

const one: TrajectoryEntry = {
  entry_id: 'a', revision: 1, kind: 'user.input', span_name: 'session.turn', slot: 'turn.input', trace_id: 't',
  span_id: 'turn', parent_span_id: null, turn_span_id: 'turn', turn_number: 1, turn_start: true, origin: 'main',
  sort_key: ['000000', 0], event_time: '2026-01-01T00:00:00Z', preview: 'hello', operation_status: 'ok',
  status_evidence: [], failure_entry: false, integrity: [], operation_start: null, operation_end: null,
  duration_ms: null, charged_ms: null, timing_basis: 'zero', duration_owner: null, meta: {},
}

let indexState: TrajectoryIndexState = READY
let recording = true

const source: TrajectorySource = {
  state: async () => ({ enabled: true, policy_revision: 1, recording_enabled: recording }),
  list: async () => ({
    epoch: 'e1', snapshot_revision: 1, entries: [one], next_cursor: null, index_state: indexState, complete: true,
  }),
  changes: async (_k, epoch, after) => ({
    epoch, from_revision: after, to_revision: after, upserts: [], removed: [], has_more: false,
    reset_required: false, index_state: indexState,
  }),
  detail: () => Promise.reject(new Error('not scripted')),
  block: () => Promise.reject(new Error('not scripted')),
}

const status = (): string[] => [...document.querySelectorAll('.trajectory-status > span')].map((s) => s.textContent ?? '')

async function draw(): Promise<void> {
  render(<TrajectoryApp />)
  await act(async () => {
    await store.refreshState()
    await store.load()
  })
}

beforeEach(() => {
  indexState = READY
  recording = true
  store._resetForTests()
  resetCapabilities()
  _resetFreshForTests()
  absorb(['trajectory-v1'])
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ trajectory: source })
  document.body.innerHTML = '<div class="chat"></div>'
  store.install()
  unpitch()
  store.sessionChanged('gui:a')
})

afterEach(() => {
  cleanup()
  resetSources()
  resetTranslator()
  document.body.innerHTML = ''
})

describe('the trajectory island', () => {
  it('draws the list between an absent status line and the bar\'s strip', async () => {
    await draw()
    const root = document.querySelector('.trajectory-root') as HTMLElement
    expect([...root.children].map((c) => c.className)).toEqual(['trajectory-body', 'trajectory-bar'])
    expect([...root.querySelector('.trajectory-body')!.children].map((c) => c.className)).toEqual(['trajectory-list'])
    expect(document.querySelectorAll('.trajectory-row')).toHaveLength(1)
  })

  it('says what the index wants said, one line each', async () => {
    indexState = { ...READY, phase: 'scanning', scanned_bytes: 2 * 1024 * 1024, total_bytes: 8 * 1024 * 1024, head_truncated: 12, failure: 'disk' }
    recording = false
    await draw()
    expect(status()).toEqual([
      'gui.trajectory.scanning {"done":"2.0","total":"8.0"}',
      'gui.trajectory.truncated {"n":12}',
      'gui.trajectory.failure {"detail":"disk"}',
      'gui.trajectory.not_recording',
    ])
    act(() => { store.liveFailed(new RpcError(-32603, 'internal')) })
    expect(status()).toContain('gui.trajectory.fault {"detail":"internal"}')
  })

  it('subscribes to the language, like every island', () => {
    const text = readFileSync('src/features/trajectory/TrajectoryApp.tsx', 'utf8') as string
    expect(text).toMatch(/useSyncExternalStore\(lang\.subscribe, lang\.get\)/)
  })
})
