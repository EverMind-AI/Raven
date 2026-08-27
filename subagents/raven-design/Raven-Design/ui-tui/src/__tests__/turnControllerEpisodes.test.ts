// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { afterEach, describe, expect, it } from 'vitest'

import type { Msg } from '../types.js'

import { turnController } from '../app/turnController.js'
import { patchUiState } from '../app/uiStore.js'

afterEach(() => {
  patchUiState({ transcript: 'legacy' })
  turnController.reset()
})

describe('turnController episodes commit', () => {
  it('buckets a turn into episodes and commits one episodes message', () => {
    patchUiState({ transcript: 'episodes' })
    turnController.reset()

    // Call 0: reason, then read two files.
    turnController.recordEpisodeStart(0)
    turnController.recordReasoningDelta('planning the reads')
    turnController.recordToolStart('a', 'read_file', 'src/approve.go')
    turnController.recordToolComplete('a', 'read_file', undefined, 'package device', 0.1)
    turnController.recordToolStart('b', 'read_file', 'src/token.go')
    turnController.recordToolComplete('b', 'read_file', undefined, 'package device 2', 0.1)
    // Call 1: the stop call — no tools, just the final answer.
    turnController.recordEpisodeStart(1)

    const { finalMessages } = turnController.recordMessageComplete({ text: 'FINAL ANSWER' })

    const epMsg = finalMessages.find(m => m.kind === 'episodes')
    expect(epMsg).toBeTruthy()
    expect(epMsg!.text).toBe('FINAL ANSWER')
    // The stop call (no tools, no reasoning) is dropped; only the work step remains.
    expect(epMsg!.episodes).toHaveLength(1)
    const [work] = epMsg!.episodes!
    expect(work!.tools.map(t => t.name)).toEqual(['read_file', 'read_file'])
    expect(work!.tools.every(t => t.ok)).toBe(true)
    expect(work!.reasoning).toContain('planning the reads')
  })

  it('records +/- line counts from an inline-diff edit', () => {
    patchUiState({ transcript: 'episodes' })
    turnController.reset()

    turnController.recordEpisodeStart(0)
    turnController.recordToolStart('e', 'edit_file', 'notes.md')
    turnController.recordInlineDiffToolComplete('--- a\n+++ b\n-old\n+new1\n+new2', 'e', 'edit_file', undefined, 0.1)
    turnController.recordEpisodeStart(1)

    const { finalMessages } = turnController.recordMessageComplete({ text: 'done' })
    const editTool = finalMessages.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!

    expect(editTool.added).toBe(2)
    expect(editTool.removed).toBe(1)
  })

  it('interrupt in episodes mode commits one collapsed episodes message, not raw segments', () => {
    patchUiState({ transcript: 'episodes' })
    turnController.reset()

    turnController.recordEpisodeStart(0)
    turnController.recordReasoningDelta('thinking about it')
    turnController.recordToolStart('a', 'web_search', 'gtm agent')
    turnController.recordToolComplete('a', 'web_search', 'boom', undefined, 0.1)

    const appended: Msg[] = []
    turnController.finalizeInterruptedTurn({ appendMessage: m => appended.push(m) })

    // Exactly one collapsed episodes message — no raw per-segment dump (the bug:
    // a Ctrl+C used to append every expanded thinking/tool segment).
    expect(appended.every(m => m.kind === 'episodes')).toBe(true)
    const epMsgs = appended.filter(m => m.kind === 'episodes')
    expect(epMsgs).toHaveLength(1)
    expect(epMsgs[0]!.episodes!).toHaveLength(1)
    expect(epMsgs[0]!.episodes![0]!.tools[0]!.name).toBe('web_search')
    expect(epMsgs[0]!.text).toContain('[interrupted]')
  })

  it('interrupt with no episode.start still keeps the legacy trail', () => {
    // An older gateway never emits episode.start. The completion path already
    // falls back to the segment trail in that case; the interrupt path must
    // too, or a Ctrl+C silently drops everything the turn had accrued.
    patchUiState({ transcript: 'episodes' })
    turnController.reset()

    turnController.recordReasoningDelta('reasoned without any episode boundary')
    turnController.recordToolStart('a', 'web_search', 'gtm agent')
    turnController.recordToolComplete('a', 'web_search', undefined, '12 results', 0.2)

    expect(turnController.episodes).toHaveLength(0)

    const appended: Msg[] = []
    turnController.finalizeInterruptedTurn({ appendMessage: m => appended.push(m) })

    expect(appended.some(m => m.kind === 'episodes')).toBe(false)
    // The legacy trail survives with the reasoning and the tool line on it...
    const trail = appended.find(m => m.kind === 'trail')
    expect(trail).toBeTruthy()
    expect(trail!.thinking).toContain('without any episode boundary')
    expect(trail!.tools!.join(' ')).toContain('Web Search')
    expect(trail!.tools!.join(' ')).toContain('12 results')
    // ...and the interruption is still recorded.
    expect(appended.some(m => (m.text ?? '').includes('[interrupted]'))).toBe(true)
  })

  it('legacy mode is fully inert: no episodes accrue and no episodes message', () => {
    patchUiState({ transcript: 'legacy' })
    turnController.reset()

    turnController.recordEpisodeStart(0)
    turnController.recordToolStart('a', 'exec', 'ls')
    turnController.recordToolComplete('a', 'exec', undefined, 'out', 0.1)

    // recordEpisodeStart is gated on episodes mode, so nothing accrued.
    expect(turnController.episodes).toHaveLength(0)

    const { finalMessages } = turnController.recordMessageComplete({ text: 'hi' })

    expect(finalMessages.some(m => m.kind === 'episodes')).toBe(false)
  })
})
