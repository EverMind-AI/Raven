// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { stringWidth } from '@hermes/ink'
import { describe, expect, it } from 'vitest'

import type { TranscriptMessage } from '../rpc/index.js'

import { DAG_TRACE_FIT_MAX_ROWS } from '../config/limits.js'
import { dagStreamTail, fitTraceTail } from '../lib/dagStream.js'
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

describe('dagStreamTail', () => {
  // The one user row a node's trace ever carries: the prompt `_with_messages`
  // prepends (raven/rpc/methods/dag.py), which the tail must not read back as
  // if the sub-agent had just said it.
  const ask = (text: string): TranscriptMessage => ({ role: 'user', text })

  it('is empty for no messages', () => {
    expect(dagStreamTail([], 40)).toBe('')
  })

  it('is empty for a transcript that is only the leading prompt', () => {
    expect(dagStreamTail([ask('audit the journal writer')], 40)).toBe('')
  })

  it('drops a leading prompt row before reading the tail of what follows', () => {
    const messages = [ask('audit the journal writer'), say('first'), say('second')]

    expect(dagStreamTail(messages, 40)).toBe('first · second')
  })

  it('reads the last thing said when it fits', () => {
    expect(dagStreamTail([say('first'), say('second')], 40)).toBe('first · second')
  })

  it('cuts to the width, keeping the newest end', () => {
    expect(dagStreamTail([say('aaaaaaaaaa'), say('bbbb')], 6)).toBe('…bbbb')
  })

  it('names a tool call the way the transcript names it', () => {
    const msg: TranscriptMessage = {
      role: 'assistant',
      text: 'checking',
      tool_calls: [{ id: 'c1', name: 'read_file', arguments: '{"path":"a/b.ts"}' }]
    }

    expect(dagStreamTail([msg], 80)).toBe('checking · Read File("a/b.ts")')
  })

  it('carries a thought, then the text, then the calls, in that order', () => {
    const msg: TranscriptMessage = {
      role: 'assistant',
      reasoning_content: 'thinking',
      text: 'saying',
      tool_calls: [{ id: 'c1', name: 'bash', arguments: '{"command":"ls"}' }]
    }

    expect(dagStreamTail([msg], 80)).toBe('thinking · saying · Bash("ls")')
  })

  it('takes a tool result as the whole of that row', () => {
    const result: TranscriptMessage = { role: 'tool', text: '42 lines', tool_call_id: 'c1' }

    expect(dagStreamTail([result], 40)).toBe('42 lines')
  })

  it('skips a message that contributes nothing', () => {
    expect(dagStreamTail([say('a'), { role: 'assistant' }, say('b')], 40)).toBe('a · b')
  })

  it('reads only as far back as the width needs', () => {
    // The assertion that the walk is from the end: a huge transcript whose head
    // could never be shown must not be visited at all.
    let touched = 0
    const messages = Array.from({ length: 400 }, (_unused, i) => {
      const text = `m${i}`

      return new Proxy({ role: 'assistant', text } as TranscriptMessage, {
        get(target, prop) {
          touched++

          return target[prop as keyof TranscriptMessage]
        }
      })
    })

    dagStreamTail(messages, 10)

    // A handful of messages at the tail, each read for a few fields -- nowhere
    // near 400.
    expect(touched).toBeLessThan(60)
  })

  it('caps a message whose text far exceeds budget and keeps the newest end', () => {
    // width=10 gives budget=40 code units. A message with text much larger than
    // that must be capped, and the result must end with the recognizable marker.
    const hugeText = 'x'.repeat(200) + 'MARKER'
    const msg: TranscriptMessage = { role: 'assistant', text: hugeText }
    const result = dagStreamTail([msg], 10)

    expect(result).toMatch(/MARKER$/)
    expect(result).toMatch(/^…/)
    expect(stringWidth(result)).toBeLessThanOrEqual(10)
  })

  it('does not orphan a surrogate when the cap lands on the low half of a pair', () => {
    // At width=10 (budget=40), this 71-unit string slices at index 31: the low
    // half of the emoji pair at 30-31. The 39-unit tail is U+200B (zero width
    // space, unlike the \s-matched U+2000-U+200A), so the whole capped slice
    // measures 0 cells and clipToWidthFromEnd -- which no-ops under budget --
    // passes the orphan through instead of clipping it away.
    const text = 'x'.repeat(30) + '\u{1F600}' + '​'.repeat(39)
    const msg: TranscriptMessage = { role: 'assistant', text }
    const result = dagStreamTail([msg], 10)

    // Positive assertions that the result is non-empty and contains expected
    // content: a purely structural scan (checking surrogate pair validity)
    // asserts nothing when there is nothing to scan, so a future change that
    // empties the result or removes the zero-width content would silently pass
    // without these checks.
    expect(result.length).toBeGreaterThan(0)
    expect(result).toContain('​')

    for (let i = 0; i < result.length; i++) {
      const code = result.charCodeAt(i)

      if (code >= 0xd800 && code <= 0xdbff) {
        const next = result.charCodeAt(i + 1)

        expect(next).toBeGreaterThanOrEqual(0xdc00)
        expect(next).toBeLessThanOrEqual(0xdfff)
      } else if (code >= 0xdc00 && code <= 0xdfff) {
        const prev = result.charCodeAt(i - 1)

        expect(prev).toBeGreaterThanOrEqual(0xd800)
        expect(prev).toBeLessThanOrEqual(0xdbff)
      }
    }
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

  it('still shows one message that is taller than the whole budget', () => {
    const huge = line(Array.from({ length: 200 }, (_unused, i) => `line ${i}`).join('\n'))
    const fit = fitTraceTail([line('older'), huge], 8, 60)

    expect(fit.shown).toEqual([huge])
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
