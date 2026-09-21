// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'

import { provMatch, provRows } from './ProviderSide'

import type { ProvFilter } from '../store'
import type { ProviderRow } from '../types'

vi.mock('../../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))

const row = (id: string, over: Partial<ProviderRow> = {}): ProviderRow =>
  ({ id, name: id, models: [], configured: [], on: false, kind: 'key', acceptsKey: true, ...over })

/* One vendor of each shape the registry ships, plus the two that tell the
   buckets apart from the credential: an aggregator reached by an endpoint
   credential, and a direct vendor reached by one. */
const rows: ProviderRow[] = [
  row('anthropic'),
  row('openrouter', { gateway: true, on: true }),
  row('custom', { kind: 'endpoint', gateway: true }),
  row('azure_openai', { kind: 'endpoint', needsBase: true }),
  row('minimax_global', { kind: 'oauth', acceptsKey: false }),
  row('ollama', { kind: 'local', needsBase: true }),
]
const SHAPES: ProvFilter[] = ['direct', 'gateway', 'oauth', 'local']
const ids = (f: ProvFilter, q = ''): string[] => provRows(rows, q, f).map((p) => p.id)

describe('the catalogue page filters', () => {
  it('file every vendor into exactly one shape bucket, an aggregator by what it resells', () => {
    expect(ids('direct')).toEqual(['anthropic', 'azure_openai'])
    expect(ids('gateway')).toEqual(['openrouter', 'custom'])
    expect(ids('oauth')).toEqual(['minimax_global'])
    expect(ids('local')).toEqual(['ollama'])
    for (const p of rows) expect(SHAPES.filter((f) => provMatch(p, '', f))).toHaveLength(1)
  })

  it('all and connected are not shapes, and a connected vendor leads its list', () => {
    expect(ids('all')).toEqual(['openrouter', 'anthropic', 'custom', 'azure_openai', 'minimax_global', 'ollama'])
    expect(ids('on')).toEqual(['openrouter'])
  })

  it('the search needle narrows any bucket by id or name, case aside', () => {
    expect(ids('gateway', 'CUST')).toEqual(['custom'])
    expect(ids('all', 'zure')).toEqual(['azure_openai'])
    expect(provMatch(rows[0]!, 'nothing', 'all')).toBe(false)
  })
})
