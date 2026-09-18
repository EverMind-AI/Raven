import { describe, expect, it } from 'vitest'

import { fromDagNode, fromSpawnContext, stepsOf } from './source'

import type { DagNodeDetail, SubagentContextResult, TranscriptMessage } from '../../rpc/generated'

describe('stepsOf', () => {
  it('reads a thought off an assistant entry', () => {
    const msgs: TranscriptMessage[] = [
      { role: 'assistant', text: '', reasoning_content: 'two dates, align first' },
    ]
    expect(stepsOf(msgs)).toEqual([{ kind: 'think', text: 'two dates, align first' }])
  })

  it('matches a tool call to its later result by tool_call_id', () => {
    const msgs: TranscriptMessage[] = [
      { role: 'assistant', text: '', tool_calls: [{ id: 'c1', name: 'web_fetch', arguments: '{"url":"x"}' }] },
      { role: 'tool', text: '200 ok', tool_call_id: 'c1' },
    ]
    expect(stepsOf(msgs)).toEqual([
      { kind: 'tool', id: 'c1', name: 'web_fetch', args: '{"url":"x"}', result: '200 ok', ok: true },
    ])
  })

  it('reads a failed tool result as not ok', () => {
    const msgs: TranscriptMessage[] = [
      { role: 'assistant', text: '', tool_calls: [{ id: 'c1', name: 'web_fetch', arguments: '{}' }] },
      { role: 'tool', text: 'Error: 429 rate limited', tool_call_id: 'c1' },
    ]
    expect(stepsOf(msgs)[0]?.kind === 'tool' && stepsOf(msgs)[0]).toMatchObject({ ok: false })
  })

  it('has no result yet for a tool call still in flight', () => {
    const msgs: TranscriptMessage[] = [
      { role: 'assistant', text: '', tool_calls: [{ id: 'c1', name: 'exec', arguments: '{}' }] },
    ]
    expect(stepsOf(msgs)).toEqual([{ kind: 'tool', id: 'c1', name: 'exec', args: '{}', result: null, ok: null }])
  })

  it('reads a role: console entry as its own step, and drops an unmatched tool row', () => {
    const msgs: TranscriptMessage[] = [
      { role: 'tool', text: 'orphaned', tool_call_id: 'nobody-called-this' },
      { role: 'console', text: 'running over the cli channel' },
    ]
    expect(stepsOf(msgs)).toEqual([{ kind: 'console', text: 'running over the cli channel' }])
  })

  it('skips a plain user or a role it does not know', () => {
    const msgs: TranscriptMessage[] = [
      { role: 'user', text: 'go' },
      { role: 'system', text: 'ignored' },
    ]
    expect(stepsOf(msgs)).toEqual([])
  })
})

describe('fromDagNode', () => {
  const detail = (over: Partial<DagNodeDetail>): DagNodeDetail => ({
    run_id: 'r1', node: 'n1', output_chars: 0, output_truncated: false, ...over,
  })

  it('prefers the rendered prompt field over the messages own first entry', () => {
    const rec = fromDagNode(detail({
      prompt: 'the real rendered prompt',
      messages: [{ role: 'user', text: 'stale' }, { role: 'assistant', text: 'done' }],
    }))
    expect(rec.dispatch).toBe('the real rendered prompt')
    expect(rec.answer).toBe('done')
  })

  it('falls back to the messages own first user entry when there is no prompt field', () => {
    const rec = fromDagNode(detail({ messages: [{ role: 'user', text: 'the dispatch' }] }))
    expect(rec.dispatch).toBe('the dispatch')
  })

  it('has no answer when the last entry is a tool call still open', () => {
    const rec = fromDagNode(detail({
      messages: [
        { role: 'user', text: 'go' },
        { role: 'assistant', text: '', tool_calls: [{ id: 'c1', name: 'exec', arguments: '{}' }] },
      ],
    }))
    expect(rec.answer).toBeNull()
    expect(rec.steps).toHaveLength(1)
  })

  it('carries output_truncated through', () => {
    expect(fromDagNode(detail({ output_truncated: true })).outputTruncated).toBe(true)
  })
})

describe('fromSpawnContext', () => {
  const ctx = (over: Partial<SubagentContextResult>): SubagentContextResult => ({ id: 'n1', messages: [], ...over })

  it('has no prompt field of its own -- the dispatch is the first message', () => {
    const rec = fromSpawnContext(ctx({ messages: [{ role: 'user', text: 'verify the quote' }] }))
    expect(rec.dispatch).toBe('verify the quote')
    expect(rec.outputTruncated).toBe(false)
  })

  it('reads the trailing plain assistant entry as the answer', () => {
    const rec = fromSpawnContext(ctx({
      messages: [{ role: 'user', text: 'go' }, { role: 'assistant', text: 'the answer' }],
    }))
    expect(rec.answer).toBe('the answer')
  })
})
