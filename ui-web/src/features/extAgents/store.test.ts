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
      seen = Object.keys(store.get().joining)
      return null
    })
    await store.act(r, 'connect')
    expect(seen).toEqual(['claude_code'])
    expect(store.get().joining).toEqual({})
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
    expect(store.get().joining).toEqual({})
  })

  /* The fix a refusal names travels with the refusal, and only when there is
     one: a write refused for any other reason keeps the entry it always had. */
  it('keeps the fix a refusal names beside its sentence', async () => {
    const r = row()
    const said = 'it is installed but has no usable credential'
    const source: ExtAgentsSource = {
      load: async () => [r],
      act: async () => {
        throw { data: { detail: said, remedy: { kind: 'sign_in', command: 'claude auth login' } } }
      },
    }
    setSources({ extAgents: source })
    await store.act(r, 'connect', {})
    expect(store.get().failed.claude_code).toEqual({
      op: 'connect',
      args: {},
      detail: said,
      remedy: { kind: 'sign_in', command: 'claude auth login' },
    })
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

describe('the model verbs', () => {
  it('sends a host pick with its provider, an agent pick alone, and a clear on its own', async () => {
    const r = row()
    const { acts } = install([r])
    await store.setModel(r, 'anthropic/claude-x', 'openrouter')
    await store.setModel(r, 'v/m')
    await store.clearModel(r)
    expect(acts).toEqual([
      ['model', 'claude_code', { model: 'anthropic/claude-x', provider: 'openrouter' }],
      ['model', 'claude_code', { model: 'v/m' }],
      ['model', 'claude_code', { clear_model: true }],
    ])
  })

  it('holds a refused pick on the row with the pick to retry, like every other write', async () => {
    const r = row()
    install([r], (op) => (op === 'model' ? 'it offers 3' : null))
    await store.setModel(r, 'v/bogus')
    expect(toastWriter.items).toEqual([])
    expect(store.get().failed.claude_code).toEqual({ op: 'model', args: { model: 'v/bogus' }, detail: 'it offers 3' })
    expect(store.get().joining).toEqual({})
  })

  it('a write that fails after another landed keeps what landed', async () => {
    /* Five Connects pressed at once: the slow one that fails must not put back
       the rows from before the fast ones connected. */
    const slow = row({ name: 'claude_code' })
    const fast = row({ name: 'codex', preset: 'codex' })
    let failSlow: (e: unknown) => void = () => {}
    const listing = [slow, { ...fast, configured: true, enabled: true }]
    setSources({
      extAgents: {
        load: async () => listing,
        act: (_op, r) => (r.name === 'claude_code'
          ? new Promise((_, reject) => { failSlow = reject })
          : Promise.resolve(listing)),
      },
    })
    store.set({ rows: [slow, fast] })

    const first = store.act(slow, 'connect')
    await store.act(fast, 'connect')
    expect(store.get().rows.find((r) => r.name === 'codex')!.enabled).toBe(true)
    failSlow({ data: { detail: 'did not answer' } })
    await first

    expect(store.get().rows.find((r) => r.name === 'codex')!.enabled).toBe(true)
    expect(store.get().failed.claude_code!.detail).toBe('did not answer')
  })

  it('keeps the rows on screen, not the ones it started from, when the listing cannot be read after a refusal', async () => {
    const r = row()
    const other = row({ name: 'codex', preset: 'codex' })
    let refuse: (e: unknown) => void = () => {}
    setSources({
      extAgents: {
        load: async () => { throw new Error('offline') },
        act: () => new Promise((_, reject) => { refuse = reject }),
      },
    })
    store.set({ rows: [r, other] })
    const pending = store.act(r, 'connect')
    store.set({ rows: [r, { ...other, enabled: true, configured: true }] })
    refuse({ data: { detail: 'refused' } })
    await pending
    expect(store.get().rows.find((x) => x.name === 'codex')!.enabled).toBe(true)
  })

  it('repaints from the listing when a pick is refused, so the sheet leaves the menu that failed behind', async () => {
    /* A row is re-measured behind the page, which is how its menu can change
       under an open sheet; a refusal is where the page finds out. */
    const held = row({ model_source: 'raven', model_choices: [] })
    const listed = row({ model_source: 'agent', model_choices: [{ value: 'v/m', name: 'M', group: 'V' }] })
    setSources({
      extAgents: {
        load: async () => [listed],
        act: async () => {
          throw { data: { detail: 'it offers 55' } }
        },
      },
    })
    store.set({ rows: [held] })

    await store.setModel(held, 'openrouter/anthropic/claude-opus-5', 'openrouter')

    expect(store.get().rows.map((r) => r.model_source)).toEqual(['agent'])
    expect(store.get().failed.claude_code).toEqual({
      op: 'model',
      args: { model: 'openrouter/anthropic/claude-opus-5', provider: 'openrouter' },
      detail: 'it offers 55',
    })
  })
})

/* The page cannot know whether the server will actually reach the agent: that
   turns on whether the value moved and whether the row is on, neither of which
   the write itself carries. So the question is "may this one be answered by
   running the agent", and the cost of the two wrong answers is not symmetric --
   over-answering puts a word on a write that returns at once, under-answering
   leaves "connecting" on a row for the length of a real ping. */
describe('probes', () => {
  it('covers every write the enable gate can answer by running the agent', () => {
    expect(store.probes({ op: 'connect', args: {} })).toBe(true)
    expect(store.probes({ op: 'migrate', args: {} })).toBe(true)
    expect(store.probes({ op: 'toggle', args: { enabled: true } })).toBe(true)
    expect(store.probes({ op: 'model', args: { model: 'v/m2' } })).toBe(true)
    expect(store.probes({ op: 'model', args: { clear_model: true } })).toBe(true)
    expect(store.probes({ op: 'update', args: { api_key: 'sk-new' } })).toBe(true)
  })

  it('leaves the writes that cannot reach the agent alone', () => {
    expect(store.probes({ op: 'toggle', args: { enabled: false } })).toBe(false)
    expect(store.probes({ op: 'update', args: { description: 'new words' } })).toBe(false)
    expect(store.probes({ op: 'update', args: { new_name: 'Renamed' } })).toBe(false)
  })
})
