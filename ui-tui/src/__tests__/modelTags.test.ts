// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import { contextLabel, tagBadge, tagLegend } from '../components/modelTags.js'

describe('the model capability badge', () => {
  it('draws the glyphs in one order whatever order the tags arrive in', () => {
    // Two models with the same tags have to produce the same badge, or the eye
    // reads a difference where the data has none.
    const a = tagBadge({ capabilities: ['file-input', 'reasoning', 'function-call'] })
    const b = tagBadge({ capabilities: ['function-call', 'file-input', 'reasoning'] })

    expect(a).toBe(b)
    expect(a).toBe('✦ƒ▤')
  })

  it('is empty for a model the registry knows nothing about', () => {
    // Absence is "unknown", not "cannot": a placeholder here would state a fact
    // nobody has.
    expect(tagBadge(undefined)).toBe('')
    expect(tagBadge({})).toBe('')
    expect(tagBadge({ capabilities: [] })).toBe('')
  })

  it('puts the window after the glyphs, and drops it when there is none', () => {
    expect(tagBadge({ capabilities: ['reasoning'], context_window: 200000 })).toBe('✦ 200K')
    expect(tagBadge({ context_window: 128000 })).toBe('128K')
    expect(tagBadge({ capabilities: ['reasoning'] })).toBe('✦')
  })

  it('rounds a window to the size a person compares', () => {
    expect(contextLabel(1_000_000)).toBe('1M')
    expect(contextLabel(2_000_000)).toBe('2M')
    expect(contextLabel(1_500_000)).toBe('1.5M')
    expect(contextLabel(200_000)).toBe('200K')
    expect(contextLabel(8192)).toBe('8K')
    expect(contextLabel(512)).toBe('512')
  })

  it('draws every glyph as one bare code point', () => {
    // What would actually break the column: an emoji (two cells wide), a
    // variation selector (which asks for the emoji rendering of a symbol that
    // is otherwise narrow), or a ZWJ sequence. Ambiguous-width symbols are not
    // on that list on purpose -- the picker's own cursor and ticks are already
    // drawn with them, and holding this row to a stricter rule than the rest of
    // the screen would buy nothing.
    const badge = tagBadge({
      capabilities: [
        'reasoning',
        'function-call',
        'structured-output',
        'image-recognition',
        'audio-recognition',
        'video-recognition',
        'file-input',
        'image-generation',
        'audio-generation',
        'video-generation',
        'embedding',
        'rerank',
        'computer-use',
      ],
    })

    expect([...badge]).toHaveLength(13)
    expect(new Set(badge).size).toBe(13)

    for (const glyph of badge) {
      const code = glyph.codePointAt(0) as number

      expect(glyph).toHaveLength(1)
      expect(code).toBeLessThan(0x10000)
      expect(code === 0xfe0f || code === 0xfe0e || code === 0x200d).toBe(false)
    }
  })
})

describe('the badge legend', () => {
  it('explains only the glyphs that are on screen', () => {
    // A terminal has nowhere to hang a tooltip, and a fixed legend of thirteen
    // symbols would be longer than the list it annotates.
    const legend = tagLegend([
      { capabilities: ['reasoning', 'function-call'] },
      { capabilities: ['function-call'] },
      undefined,
    ])

    expect(legend).toBe('✦ reasoning  ƒ tools')
  })

  it('is empty when nothing on screen carries a tag', () => {
    expect(tagLegend([])).toBe('')
    expect(tagLegend([undefined, {}])).toBe('')
  })
})
