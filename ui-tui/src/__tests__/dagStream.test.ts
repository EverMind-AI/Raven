// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { TranscriptMessage } from '../rpc/index.js'

import { DAG_TRACE_FIT_MAX_ROWS } from '../config/limits.js'
import { fitTraceTail } from '../lib/dagStream.js'
import { clipToWidthFromEnd } from '../lib/text.js'
import { estimatedMsgHeight } from '../lib/virtualHeights.js'

const say = (text: string): TranscriptMessage => ({ role: 'assistant', text })

describe('clipToWidthFromEnd', () => {
  it('returns a string that already fits, unmarked', () => {
    expect(clipToWidthFromEnd('short', 20)).toBe('short')
  })

  it('keeps the end and marks the cut at the front', () => {
    expect(clipToWidthFromEnd('abcdefghij', 5)).toBe('…ghij')
  })

  it('collapses newlines and runs of space so the result is one line', () => {
    expect(clipToWidthFromEnd('a\n\n  b\tc', 40)).toBe('a b c')
  })

  it('never half-includes a double-width cell', () => {
    // Four cells of budget, one reserved for the marker: three cells left,
    // which fits one wide char and not two.
    expect(clipToWidthFromEnd('一二三', 4)).toBe('…三')
  })

  it('is empty at a non-positive budget', () => {
    expect(clipToWidthFromEnd('abc', 0)).toBe('')
  })
})

describe('fitTraceTail', () => {
  // A user row always closes whatever fold precedes it and stands alone, so
  // each of these becomes exactly one folded message -- the 1:1 shape these
  // tests need in order to state expected counts without hand-folding the
  // fixture themselves.
  const line = (text: string): TranscriptMessage => ({ role: 'user', text })

  it('shows everything when it all fits, hiding nothing', () => {
    const msgs = [line('a'), line('b')]
    // 12, not 8: the filled prompt block costs a user row four rows of padding
    // and margin, so two of them no longer fit in the old budget and the
    // premise this states -- everything fits -- would not hold.
    const fit = fitTraceTail(msgs, 12, 60)

    expect(fit.shown).toEqual(msgs)
    expect(fit.hidden).toBe(0)
  })

  it('keeps the newest messages and counts what it dropped', () => {
    const msgs = Array.from({ length: 40 }, (_unused, i) => line(`m${i}`))
    const fit = fitTraceTail(msgs, 8, 60)

    expect(fit.shown.length).toBeLessThan(msgs.length)
    expect(fit.hidden).toBe(msgs.length - fit.shown.length)
    expect(fit.shown.at(-1)).toEqual(msgs.at(-1))
  })

  it('draws no artifact shelf, since a tail slice can only see part of the run', () => {
    const msgs: TranscriptMessage[] = [
      {
        role: 'assistant',
        text: '',
        tool_calls: [{ arguments: JSON.stringify({ path: '/tmp/report.md' }), id: 'w1', name: 'write_file' }]
      } as unknown as TranscriptMessage,
      { role: 'tool', text: 'wrote report.md' } as unknown as TranscriptMessage,
      say('Now let me check it.')
    ]

    expect(fitTraceTail(msgs, 8, 60).shown.some(msg => msg.kind === 'artifacts')).toBe(false)
  })

  it('never exceeds the row budget', () => {
    const msgs = Array.from({ length: 40 }, (_unused, i) => line(`m${i}`))
    const fit = fitTraceTail(msgs, 8, 60)
    const used = fit.shown.reduce((n, msg) => n + estimatedMsgHeight(msg, 60, { compact: false, details: false }), 0)

    expect(used).toBeLessThanOrEqual(8)
  })

  it('cuts one message that is taller than the whole budget down to it', () => {
    // The box does not truncate what overflows it -- it squeezes the column,
    // dropping scattered lines and painting the last one over the footer. So an
    // oversized message is cut here, from its head, and says so with a leading
    // ellipsis.
    const huge = line(Array.from({ length: 200 }, (_unused, i) => `line ${i}`).join('\n'))
    const fit = fitTraceTail([line('older'), huge], 8, 60)

    expect(fit.shown).toHaveLength(1)
    expect(fit.shown[0]?.text?.startsWith('…')).toBe(true)
    expect(fit.shown[0]?.text?.endsWith('line 199')).toBe(true)
    // Whatever fits, measured the way the box measures: the fold spends rows on
    // more than the lines themselves, so this is under the budget, never over.
    expect(fit.shown[0]?.text?.split('\n').length).toBeLessThanOrEqual(8)
    expect(fit.hidden).toBe(1)
  })

  it('is empty for no messages', () => {
    expect(fitTraceTail([], 8, 60)).toEqual({ hidden: 0, shown: [] })
  })

  it('stops the search at the cap instead of walking a trace that never overflows', () => {
    // No user/system row anywhere, and no narration on any assistant row: the
    // whole run folds to one un-narrated work segment regardless of how many
    // pairs it holds (segmentTurn collapses them together), so the folded
    // height never exceeds the budget and the old unbounded loop walked every
    // candidate slice up to messages.length. This is the fixture that proves
    // DAG_TRACE_FIT_MAX_ROWS is the thing that stops it, not the overflow check.
    const msgs: TranscriptMessage[] = Array.from({ length: 200 }, (_unused, i) => [
      { role: 'assistant', tool_calls: [{ id: `c${i}`, name: 'read_file', arguments: '{"path":"a.ts"}' }] },
      { role: 'tool', text: '', tool_call_id: `c${i}` }
    ]).flat()

    const fit = fitTraceTail(msgs, 8, 60)

    expect(fit.hidden).toBe(msgs.length - DAG_TRACE_FIT_MAX_ROWS)
  })
})
