// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Theme } from '../theme.js'
import type { Msg } from '../types.js'

type RenderSync = typeof renderSync

import { MessageLine } from '../components/messageLine.js'
import { hideIntroAfterFirstTurn, toTranscriptMessages } from '../domain/messages.js'
import { upsert } from '../lib/messages.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

describe('toTranscriptMessages', () => {
  it('preserves assistant tool-call rows so resume does not drop prior turns', () => {
    const rows = [
      { role: 'user', text: 'first prompt' },
      { role: 'tool', context: 'repo', name: 'search_files', text: 'ignored raw result' },
      { role: 'assistant', text: 'first answer' },
      { role: 'user', text: 'second prompt' }
    ]

    expect(toTranscriptMessages(rows).map(msg => [msg.role, msg.text])).toEqual([
      ['user', 'first prompt'],
      ['assistant', 'first answer'],
      ['user', 'second prompt']
    ])
    expect(toTranscriptMessages(rows)[1]?.tools?.[0]).toContain('Search Files')
  })
})

describe('hideIntroAfterFirstTurn', () => {
  const intro: Msg = { info: { model: 'm', skills: {}, tools: {} }, kind: 'intro', role: 'system', text: '' }

  it('keeps the cover up on a transcript that holds nothing else', () => {
    expect(hideIntroAfterFirstTurn([intro])).toEqual([intro])
  })

  it('keeps the cover up through startup notices and slash output', () => {
    const rows: Msg[] = [
      intro,
      { role: 'system', text: 'warning: credential expires soon' },
      { kind: 'panel', panelData: { sections: [], title: 'Help' }, role: 'system', text: '' }
    ]

    expect(hideIntroAfterFirstTurn(rows)).toEqual(rows)
  })

  it('drops the cover once the person has said something', () => {
    const rows: Msg[] = [intro, { role: 'user', text: 'hello' }]

    expect(hideIntroAfterFirstTurn(rows)).toEqual([{ role: 'user', text: 'hello' }])
  })

  it('drops the cover on a resumed transcript that opens with an answer', () => {
    const rows: Msg[] = [intro, { role: 'assistant', text: 'prior answer' }]

    expect(hideIntroAfterFirstTurn(rows)).toEqual([{ role: 'assistant', text: 'prior answer' }])
  })

  it('returns the same array when there is no cover to drop', () => {
    const rows: Msg[] = [{ role: 'user', text: 'hello' }]

    expect(hideIntroAfterFirstTurn(rows)).toBe(rows)
  })
})

const ESC = String.fromCharCode(27)
// Built from a char code, not a literal: an inline \x1b trips no-control-regex.
const BG_SGR = new RegExp(`${ESC}\\[(?:48;[25];|4[0-7]m|10[0-7]m)`)

// renderSync writes real escape codes to the stream, so a background fill is
// only observable here -- ink-testing-library brings its own reconciler and
// drops them.
//
// `renderSync` has to come from the same module generation as the component:
// resetModules gives @hermes/ink a fresh reconciler, and mixing generations
// renders nothing at all.
const drawWith = (renderSync: RenderSync, Line: typeof MessageLine) => (msg: Msg, t: Theme, cols = 80) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: cols, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(React.createElement(Line, { cols, msg, t }), {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  instance.unmount()
  instance.cleanup()

  // renderSync repaints the same frame on unmount; one copy is the whole frame.
  return output.split(`${ESC}[?2026h`).filter(Boolean)[0]?.split(`${ESC}[?2026l`)[0] ?? ''
}

const drawMessage = drawWith(renderSync, MessageLine)

// The fill is gated on the terminal's color tier, which @hermes/ink reads from
// the environment once at module load -- so the tier can only be chosen by
// re-importing the whole graph, the way theme.test.ts picks a scheme.
const atTier = async (level: '0' | '1' | '2' | '3') => {
  vi.stubEnv('HERMES_TUI_LEVEL', level)
  vi.resetModules()

  const ink = (await import('@hermes/ink')) as { renderSync: RenderSync }
  const { MessageLine: Line } = await import('../components/messageLine.js')
  const { resolveTheme } = await import('../theme.js')

  return { draw: drawWith(ink.renderSync, Line), resolveTheme }
}

describe('MessageLine', () => {
  it('preserves a separator after compound user prompt glyphs in transcript rows', () => {
    const t = {
      ...DEFAULT_THEME,
      brand: { ...DEFAULT_THEME.brand, prompt: 'Ψ >' }
    }

    const renderedLine = stripAnsi(drawMessage({ role: 'user', text: 'Okay' }, t))
      .split('\n')
      .find(line => line.includes('Okay'))

    expect(renderedLine).toContain('Ψ > Okay')
  })

  describe('the prompt block', () => {
    afterEach(() => {
      vi.unstubAllEnvs()
      vi.resetModules()
    })

    it('fills the whole row behind the prompt, wrapped lines included', async () => {
      const { draw, resolveTheme } = await atTier('3')
      const cols = 80
      const frame = draw({ role: 'user', text: 'x'.repeat(120) }, resolveTheme('dark', 3), cols)
      const rows = frame.split('\n').filter(row => BG_SGR.test(row))

      // Two padding rows plus the two rows the text wraps onto, every one of
      // them filled edge to edge.
      expect(rows).toHaveLength(4)

      for (const row of rows) {
        expect(stripAnsi(row)).toHaveLength(cols)
      }
    })

    it('leaves an assistant message unfilled', async () => {
      const { draw, resolveTheme } = await atTier('3')
      const frame = draw({ role: 'assistant', text: 'answer' }, resolveTheme('dark', 3))

      // Positive half first: without it, a frame that rendered nothing at all
      // would satisfy the absence of a fill.
      expect(stripAnsi(frame)).toContain('answer')
      expect(BG_SGR.test(frame)).toBe(false)
    })

    it('paints nothing where the terminal has no shade to fill with', async () => {
      const { draw, resolveTheme } = await atTier('1')
      const frame = draw({ role: 'user', text: 'Okay' }, resolveTheme('dark', 1))

      expect(stripAnsi(frame)).toContain('❯ Okay')
      expect(BG_SGR.test(frame)).toBe(false)
    })
  })
})

describe('upsert', () => {
  it('appends when last role differs', () => {
    expect(upsert([{ role: 'user', text: 'hi' }], 'assistant', 'hello')).toHaveLength(2)
  })

  it('replaces when last role matches', () => {
    expect(upsert([{ role: 'assistant', text: 'partial' }], 'assistant', 'full')[0]!.text).toBe('full')
  })

  it('appends to empty', () => {
    expect(upsert([], 'user', 'first')).toEqual([{ role: 'user', text: 'first' }])
  })

  it('does not mutate', () => {
    const prev = [{ role: 'user' as const, text: 'hi' }]
    upsert(prev, 'assistant', 'yo')
    expect(prev).toHaveLength(1)
  })
})
