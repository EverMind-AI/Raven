// @vitest-environment happy-dom
/* The page's own verbs over the shared write path: a write in flight is held
 * on the row, a refusal stays on the row, and Retry sends the same write again.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import { resetSources, setSources } from '../../state/sources'
import * as store from './store'

import type { ExtAgentActArgs, ExtAgentRow, ExtAgentsSource } from './types'

const toastWriter = vi.hoisted(() => ({ items: [] as string[] }))
vi.mock('../../state/toast', () => ({
  show: (text: string) => {
    toastWriter.items.push(text)
  },
}))

function row(over: Partial<ExtAgentRow> = {}): ExtAgentRow {
  return {
    name: 'claude_code',
    preset: 'claude_code',
    kind: 'acp',
    configured: false,
    enabled: false,
    probe_status: 'attention',
    probe_detail: '',
    has_api_key: false,
    description: 'Claude Code',
    test_running: false,
    last_test_ok: null,
    last_test_at_ms: null,
    last_test_detail: '',
    ...over,
  }
}

/* A source whose writes can be told to refuse, and which answers every call
   with the rows it holds. */
function install(rows: ExtAgentRow[], refuse: (op: string) => string | null = () => null) {
  const acts: Array<[string, string, ExtAgentActArgs]> = []
  const source: ExtAgentsSource = {
    load: async () => rows,
    act: async (op, r, args) => {
      acts.push([op, r.name, args || {}])
      const why = refuse(op)
      if (why) throw { data: { detail: why } }
      if (op === 'toggle') r.enabled = !!(args as { enabled?: boolean } | undefined)?.enabled
      return rows
    },
  }
  setSources({ extAgents: source })
  return { acts }
}

beforeEach(() => {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  toastWriter.items = []
  store._resetForTests()
})

afterEach(() => {
  resetSources()
})

describe('act', () => {
  it('holds the row on joining for the length of the write, then lets go', async () => {
    const r = row()
    let seen: string[] = []
    install([r], () => {
      seen = [...store.get().joining]
      return null
    })
    await store.act(r, 'connect')
    expect(seen).toEqual(['claude_code'])
    expect(store.get().joining).toEqual([])
    expect(store.get().failed).toEqual({})
  })

  it('keeps a refusal on the row, with the write that was refused, and does not toast it', async () => {
    const r = row()
    install([r], (op) => (op === 'connect' ? 'it did not answer a test message' : null))
    await store.act(r, 'connect', { description: 'Fixes things' })
    expect(store.get().failed.claude_code).toEqual({
      op: 'connect',
      args: { description: 'Fixes things' },
      detail: 'it did not answer a test message',
    })
    expect(toastWriter.items).toEqual([])
    expect(store.get().joining).toEqual([])
  })

  it('sends the refused write again on retry, and clears the refusal once it lands', async () => {
    const r = row()
    let refusals = 1
    const { acts } = install([r], (op) => (op === 'connect' && refusals-- > 0 ? 'no' : null))
    await store.act(r, 'connect', { description: 'x' })
    expect(store.get().failed.claude_code?.detail).toBe('no')
    store.retry(r)
    await vi.waitFor(() => expect(store.get().failed.claude_code).toBeUndefined())
    expect(acts).toEqual([
      ['connect', 'claude_code', { description: 'x' }],
      ['connect', 'claude_code', { description: 'x' }],
    ])
  })
})

describe('connectRow', () => {
  it('adds a preset, carrying the description drafted for it', async () => {
    const r = row()
    const { acts } = install([r])
    store.describe(r, 'Writes the release notes')
    expect(store.draftOf('claude_code')).toBe('Writes the release notes')
    expect(acts).toEqual([])
    store.connectRow(r)
    await vi.waitFor(() => expect(acts.length).toBe(1))
    expect(acts[0]).toEqual(['connect', 'claude_code', { description: 'Writes the release notes' }])
  })

  it('switches a configured row back on and a stale one over to its new transport', async () => {
    const off = row({ name: 'off_one', configured: true, enabled: false, probe_status: 'ready' })
    const stale = row({ name: 'stale_one', configured: true, enabled: false, probe_status: 'ready', upgrade_to: 'acp' })
    const { acts } = install([off, stale])
    store.connectRow(off)
    store.connectRow(stale)
    await vi.waitFor(() => expect(acts.length).toBe(2))
    expect(acts).toEqual([
      ['toggle', 'off_one', { enabled: true }],
      ['migrate', 'stale_one', {}],
    ])
  })

  it('writes the description of a row that exists straight away', async () => {
    const r = row({ configured: true, enabled: true, probe_status: 'ready' })
    const { acts } = install([r])
    store.describe(r, 'Now it does this')
    await vi.waitFor(() => expect(acts.length).toBe(1))
    expect(acts[0]).toEqual(['update', 'claude_code', { description: 'Now it does this' }])
    expect(store.draftOf('claude_code')).toBeUndefined()
  })
})

describe('recheck', () => {
  it('re-measures the machine and remembers a row that is still absent', async () => {
    const r = row({ probe_status: 'missing' })
    const loads: boolean[] = []
    setSources({
      extAgents: {
        load: async (probe) => {
          loads.push(!!probe)
          return [r]
        },
        act: async () => [r],
      },
    })
    await store.recheck(r)
    expect(loads).toEqual([true])
    expect(store.get().stillMissing).toEqual(['claude_code'])
    r.probe_status = 'attention'
    await store.recheck(r)
    expect(store.get().stillMissing).toEqual([])
  })
})

describe('the wizard verbs keep their toast', () => {
  it('toasts a refused connect from the wizard step', async () => {
    const r = row()
    install([r], () => 'nope')
    await store.connect(r)
    expect(toastWriter.items).toEqual(['gui.agent.failed {"detail":"nope"}'])
    expect(store.get().failed).toEqual({})
  })
})
