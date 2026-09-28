// @vitest-environment happy-dom
/* The one verdict the card's dot and the sheet's line both read, and the order
 * its facts are ranked in.
 */

import { beforeEach, describe, expect, it } from 'vitest'

import { setTranslator } from '../../i18n/t'
import { healthOf } from './health'
import * as store from './store'

import type { ExtAgentsState } from './store'
import type { ExtAgentRow } from './types'

function row(over: Partial<ExtAgentRow> = {}): ExtAgentRow {
  return {
    name: 'codex',
    preset: 'codex',
    kind: 'acp',
    configured: true,
    enabled: true,
    probe_status: 'ready',
    probe_detail: '',
    has_api_key: false,
    description: '',
    test_running: false,
    last_test_ok: null,
    last_test_at_ms: null,
    last_test_detail: '',
    ...over,
  }
}

const state = (over: Partial<ExtAgentsState> = {}): ExtAgentsState => ({ ...store.get(), ...over })
const health = (r: ExtAgentRow, s: ExtAgentsState = state()) => healthOf(r, s)

beforeEach(() => {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  store._resetForTests()
})

describe('healthOf', () => {
  /* Every fact at once, peeled one at a time: the order is the policy. */
  it('ranks a write in flight over a refusal, over a failed test, over a caveat, over on', () => {
    const r = row({ probe_status: 'attention', last_test_ok: false, last_test_detail: 'no credential' })
    const refusal = { op: 'connect' as const, args: {}, detail: 'refused' }
    expect(health(r, state({ joining: { codex: { op: 'connect', args: {} } }, failed: { codex: refusal } })))
      .toMatchObject({ tone: 'busy', label: 'gui.agent.testing' })
    expect(health(r, state({ failed: { codex: refusal } }))).toMatchObject({ tone: 'bad', from: 'write' })
    expect(health(r)).toMatchObject({ tone: 'bad', from: 'test' })
    expect(health(row({ probe_status: 'attention' }))).toMatchObject({ tone: 'warn', label: 'gui.agent.hd_on_attention' })
    expect(health(row()).tone).toBe('good')
  })

  it('counts a test under way as busy, whichever side started it', () => {
    expect(health(row({ test_running: true })).label).toBe('gui.agent.testing_head')
    expect(health(row(), state({ testing: ['codex'] })).tone).toBe('busy')
  })

  it('does not doubt the built-in loop', () => {
    expect(health(row({ kind: 'builtin', builtin: true, configured: false, probe_status: 'attention' })).tone).toBe('good')
  })

  /* "Not connected" is the section's word. A failed test on such a row is still
     a verdict the sheet reports; "not found" outranks it. */
  it('says nothing for a row that is off or absent, but keeps a failed test on an off row', () => {
    expect(health(row({ enabled: false })).tone).toBe('none')
    expect(health(row({ kind: 'cli', enabled: false, probe_status: 'missing' })).tone).toBe('none')
    expect(health(row({ enabled: false, last_test_ok: false }))).toMatchObject({ tone: 'bad', from: 'test' })
    expect(health(row({ kind: 'cli', enabled: false, probe_status: 'missing', last_test_ok: false })).tone).toBe('none')
  })

  it('says of a working row whether it was tested', () => {
    expect(health(row()).label).toContain('gui.agent.hd_on_by')
    expect(health(row({ last_test_ok: true })).label).toBe('gui.agent.hd_on_tested')
  })

  /* The probe's caveats are about now -- a launch config changed since that
     test, a menu never measured, a model no longer listed -- so a pass from
     before does not answer them; the next test does, and its re-read probes. */
  it('keeps a caveat over a test that passed before it', () => {
    expect(health(row({ probe_status: 'attention', last_test_ok: true }))).toMatchObject({ tone: 'warn', label: 'gui.agent.hd_on_attention' })
  })

  it('names a failed test in the reader\'s words, with or without the server\'s sentence', () => {
    expect(health(row({ last_test_ok: false, last_test_detail: 'no credential' })).label).toBe('gui.agent.hd_on_test_bad')
    expect(health(row({ last_test_ok: false })).label).toBe('gui.agent.hd_on_test_bad')
  })
})
