import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetCapabilities } from '../../rpc/capabilities'
import { FixtureTransport } from '../../rpc/fixtureTransport'
import { setGateway } from '../../rpc/gateway'
import { RpcError } from '../../rpc/transport'
import { CHANGES_BATCH, LIST_PAGE, isAbsent, isCursorExpired, isDisabled, trajectorySource } from './source'

let transport: FixtureTransport

beforeEach(() => {
  transport = new FixtureTransport({
    'trajectory.state': { enabled: true, policy_revision: 1, recording_enabled: true },
    'trajectory.list': (p) => ({
      epoch: 'e', snapshot_revision: 0, entries: [], next_cursor: p.cursor ?? null,
      index_state: {
        phase: 'ready', scanned_bytes: 0, total_bytes: 0, head_truncated: 0, recovering_traces: 0,
        unresolved_traces: 0, unresolved_dropped: 0, oversized_lines_dropped: 0, preview_pending: 0, failure: null,
      },
      complete: true,
    }),
    'trajectory.changes': (p) => ({
      epoch: p.epoch, from_revision: p.after_revision, to_revision: p.after_revision, upserts: [], removed: [],
      has_more: false, reset_required: false,
      index_state: {
        phase: 'ready', scanned_bytes: 0, total_bytes: 0, head_truncated: 0, recovering_traces: 0,
        unresolved_traces: 0, unresolved_dropped: 0, oversized_lines_dropped: 0, preview_pending: 0, failure: null,
      },
    }),
  })
  setGateway(transport)
})

afterEach(() => {
  resetCapabilities()
})

describe('the trajectory source', () => {
  it('sends the session key with the page sizes the view is built around', async () => {
    await trajectorySource.state()
    await trajectorySource.list('gui:a')
    await trajectorySource.list('gui:a', 'c2')
    await trajectorySource.changes('gui:a', 'e', 7)
    expect(transport.calls).toEqual([
      { method: 'trajectory.state', params: {} },
      { method: 'trajectory.list', params: { session_key: 'gui:a', cursor: null, limit: LIST_PAGE } },
      { method: 'trajectory.list', params: { session_key: 'gui:a', cursor: 'c2', limit: LIST_PAGE } },
      { method: 'trajectory.changes', params: { session_key: 'gui:a', epoch: 'e', after_revision: 7, limit: CHANGES_BATCH } },
    ])
    expect(LIST_PAGE).toBe(200)
    expect(CHANGES_BATCH).toBe(500)
  })

  it('tells a switched-off view from an absent surface from a plain failure', () => {
    expect(isDisabled(new RpcError(-32020, 'off'))).toBe(true)
    expect(isDisabled(new RpcError(-32601, 'no'))).toBe(false)
    expect(isDisabled(new Error('socket'))).toBe(false)
    expect(isCursorExpired(new RpcError(-32022, 'gone'))).toBe(true)
    expect(isCursorExpired(new RpcError(-32020, 'off'))).toBe(false)
    expect(isAbsent(new Error('socket'))).toBe(false)
    expect(isAbsent(new RpcError(-32020, 'off'))).toBe(false)
    expect(isAbsent(new RpcError(-32601, 'no'))).toBe(true)
    /* Once refused, remembered: the next question needs no second failed call. */
    expect(isAbsent(new Error('socket'))).toBe(true)
  })
})
