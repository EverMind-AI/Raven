import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetCapabilities } from '../../rpc/capabilities'
import { FixtureTransport } from '../../rpc/fixtureTransport'
import { setGateway } from '../../rpc/gateway'
import { RpcError } from '../../rpc/transport'
import {
  CHANGES_BATCH, LIST_PAGE, isAbsent, isCursorExpired, isDisabled, isEntryGone, isUnknownBlock, revisionChange, trajectorySource,
} from './source'

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
    'trajectory.detail': (p) => ({
      session_key: p.session_key, epoch: 'e', entry_id: p.entry_id, entry_revision: p.entry_revision ?? 1, kind: 'user.input',
      span_name: 'session.turn', slot: 'turn.input', operation_status: 'ok', status_evidence: [], failure_entry: false,
      integrity: [], notes: [], blocks: [], revision_changed: false, truncated: false,
    }),
    'trajectory.block': (p) => ({
      entry_id: p.entry_id, entry_revision: p.entry_revision, epoch: p.epoch, block_id: p.block_id, renderer: 'text',
      availability: 'available', reason: null, data: { text: '' }, next_cursor: null, total_items: null, integrity: [], truncated: false,
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
    await trajectorySource.detail('gui:a', 'x')
    await trajectorySource.detail('gui:a', 'x', 3)
    await trajectorySource.block('gui:a', 'x', 3, 'e', 'content')
    await trajectorySource.block('gui:a', 'x', 3, 'e', 'messages', 'c1')
    expect(transport.calls).toEqual([
      { method: 'trajectory.state', params: {} },
      { method: 'trajectory.list', params: { session_key: 'gui:a', cursor: null, limit: LIST_PAGE } },
      { method: 'trajectory.list', params: { session_key: 'gui:a', cursor: 'c2', limit: LIST_PAGE } },
      { method: 'trajectory.changes', params: { session_key: 'gui:a', epoch: 'e', after_revision: 7, limit: CHANGES_BATCH } },
      { method: 'trajectory.detail', params: { session_key: 'gui:a', entry_id: 'x', entry_revision: null } },
      { method: 'trajectory.detail', params: { session_key: 'gui:a', entry_id: 'x', entry_revision: 3 } },
      { method: 'trajectory.block', params: { session_key: 'gui:a', entry_id: 'x', entry_revision: 3, epoch: 'e', block_id: 'content', cursor: null } },
      { method: 'trajectory.block', params: { session_key: 'gui:a', entry_id: 'x', entry_revision: 3, epoch: 'e', block_id: 'messages', cursor: 'c1' } },
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

  it('tells a gone entry from an unknown block, and reads where a moved entry went', () => {
    expect(isEntryGone(new RpcError(-32021, 'entry_not_found'))).toBe(true)
    expect(isEntryGone(new RpcError(-32021, 'entry_not_found', { block_id: 'nope' }))).toBe(false)
    expect(isUnknownBlock(new RpcError(-32021, 'entry_not_found', { block_id: 'nope' }))).toBe(true)
    expect(isUnknownBlock(new RpcError(-32021, 'entry_not_found'))).toBe(false)
    expect(revisionChange(new RpcError(-32023, 'moved', { current_revision: 9, current_epoch: 'e2', detail: 'x' })))
      .toEqual({ revision: 9, epoch: 'e2' })
    expect(revisionChange(new RpcError(-32023, 'moved'))).toBeNull()
    expect(revisionChange(new RpcError(-32021, 'gone'))).toBeNull()
  })
})
