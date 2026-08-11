import { describe, expect, it } from 'vitest'

import { asRpcResult, rpcErrorMessage } from '../lib/rpc.js'
import { RpcError, rpcErrorFromFrame, SessionNotFoundError } from '../rpc/errors.js'

describe('asRpcResult', () => {
  it('keeps plain object payloads', () => {
    expect(asRpcResult({ ok: true, value: 'x' })).toEqual({ ok: true, value: 'x' })
  })

  it('rejects missing or non-object payloads', () => {
    expect(asRpcResult(undefined)).toBeNull()
    expect(asRpcResult(null)).toBeNull()
    expect(asRpcResult('oops')).toBeNull()
    expect(asRpcResult(['bad'])).toBeNull()
  })
})

describe('rpcErrorMessage', () => {
  it('prefers Error messages', () => {
    expect(rpcErrorMessage(new Error('boom'))).toBe('boom')
  })

  it('falls back for unknown errors', () => {
    expect(rpcErrorMessage('broken')).toBe('broken')
    expect(rpcErrorMessage({ code: 500 })).toBe('request failed')
  })
})

describe('rpcErrorFromFrame', () => {
  it('keeps the bare code name when the frame carries no context', () => {
    const err = rpcErrorFromFrame({ code: -32603, message: 'internal_error' })
    expect(err.message).toBe('[rpc -32603] internal_error')
  })

  it('surfaces the cause the server put in data, plus where to read more', () => {
    const err = rpcErrorFromFrame({
      code: -32603,
      message: 'internal_error',
      data: {
        reason: 'tui_init_crash',
        detail: 'Config at ~/.raven/config.json fails schema validation',
        log_path: '~/.raven/logs/tui.log'
      }
    })
    expect(err.message).toBe(
      '[rpc -32603] internal_error: Config at ~/.raven/config.json fails schema validation\n' +
        '(details in ~/.raven/logs/tui.log)'
    )
  })

  it('reads exception_message and reason when detail is absent', () => {
    expect(
      rpcErrorFromFrame({ code: -32603, message: 'internal_error', data: { exception_message: 'boom' } }).message
    ).toBe('[rpc -32603] internal_error: boom')
    expect(rpcErrorFromFrame({ code: -32603, message: 'internal_error', data: { reason: 'uncaught' } }).message).toBe(
      '[rpc -32603] internal_error: uncaught'
    )
  })

  it('keeps a multi-line cause readable below the summary', () => {
    const err = rpcErrorFromFrame({
      code: -32011,
      message: 'config_validation_error',
      data: { detail: '2 validation errors\nsubagents: extra inputs are not permitted' }
    })
    expect(err.message).toBe(
      '[rpc -32011] config_validation_error:\n2 validation errors\nsubagents: extra inputs are not permitted'
    )
  })

  it('ignores non-object and blank data without losing the code name', () => {
    for (const data of [undefined, null, ['detail'], 'detail', { detail: '   ' }, { detail: 7 }]) {
      expect(rpcErrorFromFrame({ code: -32603, message: 'internal_error', data }).message).toBe(
        '[rpc -32603] internal_error'
      )
    }
  })

  it('still selects the typed subclass and exposes raw data', () => {
    const err = rpcErrorFromFrame({ code: -32001, message: 'session_not_found', data: { detail: 'no such key' } })
    expect(err).toBeInstanceOf(SessionNotFoundError)
    expect(err).toBeInstanceOf(RpcError)
    expect(err.code).toBe(-32001)
    expect(err.data).toEqual({ detail: 'no such key' })
  })
})
