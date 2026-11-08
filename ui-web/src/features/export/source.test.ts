import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetCapabilities } from '../../rpc/capabilities'
import { FixtureTransport } from '../../rpc/fixtureTransport'
import { setGateway } from '../../rpc/gateway'
import { RpcError } from '../../rpc/transport'
import { exportSource, invalidSelection, isDisabled, isGone, isLimit, messageOf } from './source'

let transport: FixtureTransport

beforeEach(() => {
  transport = new FixtureTransport({
    'trajectory.turns': { title: 't', turns: [] },
    'trajectory.export': { job_id: 'exj-1' },
    'trajectory.export_status': (p) => ({ job_id: p.job_id, state: 'running', phase: 'collecting', file_name: 'a.tar.gz' }),
    'trajectory.export_cancel': { ok: true },
  })
  setGateway(transport)
})

afterEach(() => {
  resetCapabilities()
})

describe('the export source', () => {
  it('sends snake_case params for every call', async () => {
    await exportSource.turns('gui:a')
    await exportSource.start('gui:a', ['t1', 't2'], 'name.tar.gz')
    await exportSource.status('exj-1')
    await exportSource.cancel('exj-1')
    expect(transport.calls).toEqual([
      { method: 'trajectory.turns', params: { session_key: 'gui:a' } },
      { method: 'trajectory.export', params: { session_key: 'gui:a', trace_ids: ['t1', 't2'], file_name: 'name.tar.gz' } },
      { method: 'trajectory.export_status', params: { job_id: 'exj-1' } },
      { method: 'trajectory.export_cancel', params: { job_id: 'exj-1' } },
    ])
  })
})

describe('reading the refusals', () => {
  const refusal = (data: unknown, code = -32602): RpcError => new RpcError(code, 'refused', data)

  it('reads an invalid selection turn by turn', () => {
    const err = refusal({ reason: 'invalid_selection', invalid: [{ trace_id: 't1', why: 'in_progress' }, { bad: 1 }] })
    expect(invalidSelection(err)).toEqual({ invalid: [{ trace_id: 't1', why: 'in_progress' }], why: null })
    expect(invalidSelection(refusal({ reason: 'invalid_selection', invalid: [], why: 'empty' })))
      .toEqual({ invalid: [], why: 'empty' })
  })

  it('takes no other invalid-params answer, and no other code, for one', () => {
    expect(invalidSelection(refusal({ reason: 'something_else' }))).toBeNull()
    expect(invalidSelection(refusal(undefined))).toBeNull()
    expect(invalidSelection(refusal({ reason: 'invalid_selection' }, -32603))).toBeNull()
    expect(invalidSelection(new Error('plain'))).toBeNull()
  })

  it('names the policy, the vanished job and the limit by their codes', () => {
    expect(isDisabled(new RpcError(-32020, 'off'))).toBe(true)
    expect(isGone(new RpcError(-32024, 'gone'))).toBe(true)
    expect(isLimit(new RpcError(-32025, 'busy'))).toBe(true)
    expect(isLimit(new RpcError(-32024, 'gone'))).toBe(false)
    expect(messageOf(new RpcError(-32000, 'boom'))).toBe('boom')
    expect(messageOf('raw')).toBe('raw')
  })
})
