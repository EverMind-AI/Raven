// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { describe, expect, it } from 'vitest'

import { VERBS } from '../content/verbs.js'
import { hasMeaningfulReasoning, hasReasoningTag, splitReasoning } from '../lib/reasoning.js'
import { cleanThinkingText } from '../lib/text.js'

describe('splitReasoning', () => {
  it('extracts <think>…</think> and strips it from text', () => {
    const { reasoning, text } = splitReasoning('<think>plotting</think>\n\nhere is the answer')

    expect(reasoning).toBe('plotting')
    expect(text).toBe('here is the answer')
  })

  it('handles multiple tag shapes', () => {
    const input = '<reasoning>a</reasoning> <THINKING>b</THINKING> <thought>c</thought> body'
    const { reasoning, text } = splitReasoning(input)

    expect(reasoning).toContain('a')
    expect(reasoning).toContain('b')
    expect(reasoning).toContain('c')
    expect(text).toBe('body')
  })

  it('treats unclosed trailing <think>… as reasoning', () => {
    const { reasoning, text } = splitReasoning('answer start <think>still deciding')

    expect(reasoning).toBe('still deciding')
    expect(text).toBe('answer start')
  })

  it('returns empty reasoning and untouched text when no tags present', () => {
    const { reasoning, text } = splitReasoning('plain body with no tags')

    expect(reasoning).toBe('')
    expect(text).toBe('plain body with no tags')
  })

  it('preserves text when reasoning block is empty', () => {
    const { reasoning, text } = splitReasoning('<think></think>only body')

    expect(reasoning).toBe('')
    expect(text).toBe('only body')
  })

  it('detects presence of any supported tag', () => {
    expect(hasReasoningTag('pre <think>x</think> post')).toBe(true)
    expect(hasReasoningTag('pre <reasoning>x</reasoning>')).toBe(true)
    expect(hasReasoningTag('<REASONING_SCRATCHPAD>x</REASONING_SCRATCHPAD>')).toBe(true)
    expect(hasReasoningTag('no tags at all')).toBe(false)
  })
})

describe('cleanThinkingText', () => {
  it('removes face/status ticker fragments while preserving real reasoning', () => {
    expect(
      cleanThinkingText(
        '(¬_¬) synthesizing...**Resolving comments on GitHub**\n( ͡° ͜ʖ ͡°) musing...\nActual step\n٩(๑❛ᴗ❛๑)۶ contemplating...next step'
      )
    ).toBe('**Resolving comments on GitHub**\nActual step\nnext step')
  })

  it('keeps prose in front of a verb word when no face precedes it', () => {
    // A ticker fragment is a face glyph followed by its verb. Treating any run
    // of non-letters as the face deleted the sentence ahead of every verb-like
    // word, which on non-Latin reasoning is most of the line.
    expect(cleanThinkingText('先看调用顺序 pondering... 再看锁的粒度')).toBe(
      '先看调用顺序 pondering... 再看锁的粒度'
    )
    expect(cleanThinkingText('the queue is processing the tail')).toBe('the queue is processing the tail')
  })

  it('still drops a bare verb line', () => {
    expect(cleanThinkingText('musing...\nthe real thought')).toBe('the real thought')
  })

  it("keeps Raven's own ticker words, which are not what leaks", () => {
    // The filter reads `tickerNoise`, not `VERBS`. Sharing one list meant a
    // rename of the status ticker silently changed what gets censored out of a
    // model's reasoning -- and `tracing` is a word a model writes for real.
    for (const verb of VERBS) {
      expect(cleanThinkingText(`${verb}...\nthe real thought`)).toBe(`${verb}...\nthe real thought`)
    }
  })

  it('stays linear on a long non-Latin buffer', () => {
    // Guards the shape of the match, not the machine's speed: the pattern this
    // replaced was quadratic in line length, so a full reasoning buffer of CJK
    // cost hundreds of ms per call at ~60 calls/s and wedged the event loop.
    // The bound is loose enough not to flake on a busy CI box.
    const paragraph = '用户想让我分析一下这个接口的调用顺序和锁的粒度'.repeat(50)
    const buffer = Array.from({ length: 60 }, () => paragraph).join('\n')

    expect(buffer.length).toBeGreaterThan(60_000)

    const started = performance.now()

    cleanThinkingText(buffer)

    expect(performance.now() - started).toBeLessThan(100)
  })
})

describe('hasMeaningfulReasoning', () => {
  it('rejects placeholder bursts that carry no words', () => {
    // Some models emit reasoning_content that is only dots during a mechanical
    // tool loop; showing that as thought is noise.
    expect(hasMeaningfulReasoning('.\n.\n.')).toBe(false)
    expect(hasMeaningfulReasoning('')).toBe(false)
    expect(hasMeaningfulReasoning('   ')).toBe(false)
  })

  it('accepts words in any script', () => {
    expect(hasMeaningfulReasoning('weighing the options')).toBe(true)
    expect(hasMeaningfulReasoning('step 2')).toBe(true)
  })
})
