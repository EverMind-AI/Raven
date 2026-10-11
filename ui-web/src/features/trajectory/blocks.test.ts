// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import {
  BLOCK_IDS, availabilityKey, basisKey, blockTitleKey, evidenceKey, integrityKey, noteKey, previewSummary, reasonKey,
  statusKey, timingLabelKey,
} from './blocks'

import type { TrajectoryBlockDescriptor } from './types'

const catalogue = (): Record<string, unknown> =>
  (JSON.parse(readFileSync('../i18n/messages.json', 'utf8') as string) as { ui: Record<string, unknown> }).ui

const block = (over: Partial<TrajectoryBlockDescriptor>): TrajectoryBlockDescriptor => ({
  id: 'content', renderer: 'text', availability: 'available', preview: null, total_items: null, related_operation: null,
  reason: null, ...over,
})

describe('the detail vocabulary', () => {
  it('names every block the registry can produce, and the catalogue carries each name', () => {
    const ui = catalogue()
    /* The backend's own tables: the per-kind blocks plus the common tail
       (raven/trajectory/details.py). */
    const expected = [
      'content', 'media', 'origin', 'capabilities', 'turn', 'messages', 'system', 'prompt', 'model', 'tools', 'request',
      'thinking', 'thinkingBlocks', 'toolCalls', 'finish', 'usage', 'response', 'tool', 'params', 'schema', 'result',
      'skill', 'skills', 'stats', 'query', 'decision', 'task', 'candidates', 'agent', 'settings', 'hits', 'injected',
      'used', 'position', 'operation', 'qa', 'summary', 'plugin', 'contribution', 'transcript', 'frames', 'attributes',
      'error', 'timing', 'relations', 'integrity', 'raw', 'outline',
    ]
    expect([...BLOCK_IDS].sort()).toEqual([...expected].sort())
    for (const id of BLOCK_IDS) expect(ui[blockTitleKey(id) as string], id).toBeTruthy()
    expect(blockTitleKey('something_new')).toBeNull()
  })

  it('has a sentence in the catalogue for every code it knows, and none for one it does not', () => {
    const ui = catalogue()
    const known: Array<[string[], (code: string) => string | null]> = [
      [['empty', 'not_recorded', 'missing', 'truncated', 'unreadable', 'unsupported'], availabilityKey],
      [['not_loaded', 'schema_unproven', 'preview_dropped', 'not_recorded', 'artifact_missing', 'artifact_unreadable', 'artifact_outside_store', 'artifact_truncated'], reasonKey],
      [['artifact_outside_store', 'artifact_missing', 'artifact_unreadable', 'artifact_truncated', 'blob_missing', 'blob_truncated'], integrityKey],
      [['outer_only', 'in_progress_snapshot', 'data_incomplete'], noteKey],
      [['running', 'ok', 'error', 'cancelled', 'unknown'], statusKey],
      [['span_error', 'tool_error', 'tool_result_error', 'outer_only'], evidenceKey],
      [['zero', 'span_full', 'shared', 'not_recorded', 'unknown'], basisKey],
      [['event_time', 'operation_start', 'operation_end', 'duration_ms', 'charged_ms', 'duration_owner'], timingLabelKey],
    ]
    for (const [codes, lookup] of known) {
      for (const code of codes) expect(ui[lookup(code) as string], code).toBeTruthy()
      expect(lookup('no_such_code')).toBeNull()
    }
    expect(availabilityKey('available')).toBeNull()
  })

  it('reads the backend\'s clipping marks off a preview', () => {
    expect(previewSummary(block({ renderer: 'text', preview: 'short' }))).toEqual({ clipped: false, total: null, shown: null })
    expect(previewSummary(block({ renderer: 'text', preview: 'a long one…' }))).toEqual({ clipped: true, total: null, shown: null })
    expect(previewSummary(block({ renderer: 'json', preview: { a: 1, $more_keys: 4 } }))).toEqual({ clipped: true, total: null, shown: null })
    expect(previewSummary(block({ renderer: 'json', preview: { a: 1 } })).clipped).toBe(false)
    expect(previewSummary(block({ renderer: 'messages', preview: [{ role: 'user' }, { role: 'assistant' }], total_items: 7 })))
      .toEqual({ clipped: true, total: 7, shown: 2 })
    expect(previewSummary(block({ renderer: 'items', preview: ['a', 'b'], total_items: 2 }))).toEqual({ clipped: false, total: 2, shown: 2 })
    expect(previewSummary(block({ renderer: 'key_values', preview: [{ key: 'k', value: 1 }], total_items: 1 })).clipped).toBe(false)
    expect(previewSummary(block({ renderer: 'text', preview: null, reason: 'preview_dropped' }))).toEqual({ clipped: true, total: null, shown: null })
  })
})
