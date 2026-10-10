// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'

import * as citations from './citations'

/* What a knowledge search found, kept beside the call that found it.
 *
 * The channel is shared: `deliver_files` files a manifest on the same one. So
 * most of what these cover is what this registry does NOT take.
 */

beforeEach(() => {
  citations._resetForTests()
})

const hit = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  base_id: 'kb-a',
  document_id: 'handbook',
  source: 'handbook.pdf',
  page: 12,
  chunk_index: 4,
  ...over,
})

describe('what a search filed', () => {
  it('keeps the passages under the call that found them', () => {
    citations.record('call-1', { knowledge_hits: [hit()] })

    expect(citations.of('call-1')).toEqual([
      { baseId: 'kb-a', documentId: 'handbook', source: 'handbook.pdf', page: 12, chunkIndex: 4 },
    ])
  })

  it('takes nothing from another tool\'s payload on the same channel', () => {
    /* `deliver_files` files a manifest here. A payload that is not this one's
       is not this one's business. */
    citations.record('call-1', { raven_delivery: { files: [{ path: '/tmp/out.pdf' }] } })

    expect(citations.of('call-1')).toEqual([])
  })

  it('answers nothing for a call that found nothing', () => {
    expect(citations.of('call-9')).toEqual([])
    expect(citations.of(null)).toEqual([])
    expect(citations.of(undefined)).toEqual([])
  })

  it('drops a row with nothing to open', () => {
    /* A document id is the whole of what makes a citation pressable. */
    citations.record('call-1', { knowledge_hits: [hit({ document_id: '' }), hit()] })

    expect(citations.of('call-1')).toHaveLength(1)
  })

  it('reads a format with no pages as having none, not as page zero', () => {
    citations.record('call-1', { knowledge_hits: [hit({ page: null })] })

    expect(citations.of('call-1')[0]!.page).toBeNull()
  })

  it('falls back to the id where a document has no name', () => {
    citations.record('call-1', { knowledge_hits: [hit({ source: undefined })] })

    expect(citations.of('call-1')[0]!.source).toBe('handbook')
  })

  it('ignores a payload that is not one', () => {
    citations.record('call-1', null)
    citations.record('call-2', 'hits')
    citations.record('call-3', { knowledge_hits: 'one' })
    citations.record('', { knowledge_hits: [hit()] })

    expect(citations.of('call-1')).toEqual([])
    expect(citations.of('call-2')).toEqual([])
    expect(citations.of('call-3')).toEqual([])
  })

  it('keeps the newest calls and lets the oldest go', () => {
    /* A long conversation can run hundreds of searches, and what a reader can
       press is what is on screen. */
    for (let at = 0; at < 260; at += 1) citations.record(`call-${at}`, { knowledge_hits: [hit()] })

    expect(citations.of('call-0')).toEqual([])
    expect(citations.of('call-259')).toHaveLength(1)
  })

  it('replaces what one call filed rather than adding to it', () => {
    /* One call, one answer: a retry of the same call is the same call. */
    citations.record('call-1', { knowledge_hits: [hit(), hit({ document_id: 'runbook' })] })
    citations.record('call-1', { knowledge_hits: [hit({ document_id: 'notes' })] })

    expect(citations.of('call-1').map((row) => row.documentId)).toEqual(['notes'])
  })
})
