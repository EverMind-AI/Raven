// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { renderSync } from '@hermes/ink'
import { render } from 'ink-testing-library'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it, vi } from 'vitest'

import type { ClarifyReq } from '../types.js'

import { ClarifyPrompt } from '../components/prompts.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const req = (over: Partial<ClarifyReq> = {}): ClarifyReq => ({
  choices: ['hold', 'ship'],
  question: 'Ship it?',
  requestId: 'r1',
  ...over
})

const noop = () => {}

const frameOf = (node: React.ReactElement): string => stripAnsi(render(node).lastFrame() ?? '')

// ink only routes keystrokes through useInput when stdin looks like a raw-mode
// TTY, which ink-testing-library's stub stdin is not -- so the typing tests get
// the same PassThrough harness the model-picker tests use.
const driven = (onAnswer: (s: string) => void, over: Partial<ClarifyReq> = {}) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: noop, setRawMode: noop, unref: noop })
  Object.assign(stderr, { isTTY: true })

  const instance = renderSync(
    <ClarifyPrompt onAnswer={onAnswer} onCancel={noop} req={req(over)} t={DEFAULT_THEME} />,
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  return {
    // One keystroke per write: ink's input parser reads a multi-character write
    // as a single burst, which the text input does not treat as typing.
    key: async (s: string) => {
      stdin.write(s)
      await delay(30)
    },
    type: async (s: string) => {
      for (const ch of s) {
        stdin.write(ch)
        await delay(15)
      }
      await delay(20)
    },
    unmount: () => {
      instance.unmount()
      instance.cleanup()
    }
  }
}

const DOWN = "\u001B[B"
const TAB = '\t'
const ENTER = '\r'

describe('ClarifyPrompt', () => {
  it('shows the short header beside the question', () => {
    const frame = frameOf(
      <ClarifyPrompt onAnswer={noop} onCancel={noop} req={req({ header: 'Release' })} t={DEFAULT_THEME} />
    )

    expect(frame).toContain('Release')
    expect(frame).toContain('Ship it?')
  })

  it('shows the batch position and the rest of the set', () => {
    const frame = frameOf(
      <ClarifyPrompt
        onAnswer={noop}
        onCancel={noop}
        req={req({
          batch: [
            { header: 'Base', question: 'Base branch?' },
            { header: 'Release', question: 'Ship it?' },
            { header: 'Squash', question: 'Squash?' }
          ],
          index: 1,
          total: 3
        })}
        t={DEFAULT_THEME}
      />
    )

    expect(frame).toContain('2/3')
    // Seeing the whole set is what tells the user how much is still coming.
    expect(frame).toContain('Base branch?')
    expect(frame).toContain('Squash?')
  })

  it('marks the option the agent recommends', () => {
    const frame = frameOf(
      <ClarifyPrompt onAnswer={noop} onCancel={noop} req={req({ recommended: 'ship' })} t={DEFAULT_THEME} />
    )

    expect(frame).toMatch(/ship.*recommended/i)
  })

  it('shows how long the question will stand', () => {
    const frame = frameOf(
      <ClarifyPrompt onAnswer={noop} onCancel={noop} req={req({ timeoutS: 90 })} t={DEFAULT_THEME} />
    )

    expect(frame).toMatch(/1m 30s/)
  })

  it('sends the selected option together with the note the user added', async () => {
    const onAnswer = vi.fn()
    const h = driven(onAnswer)

    await h.key(DOWN)
    await h.key(TAB)
    await h.type('needs a changelog entry')
    await h.key(ENTER)
    h.unmount()

    expect(onAnswer).toHaveBeenCalledWith('ship (note: needs a changelog entry)')
  })

  it('still sends a bare option when no note is added', async () => {
    const onAnswer = vi.fn()
    const h = driven(onAnswer)

    await h.key(ENTER)
    h.unmount()

    expect(onAnswer).toHaveBeenCalledWith('hold')
  })

  it('quick-picks the Other row by its own number', async () => {
    const onAnswer = vi.fn()
    const h = driven(onAnswer)

    // The Other row is rendered as "3." beside two choices, so 3 has to reach
    // it -- otherwise the list numbers a row the keyboard cannot select.
    await h.key('3')
    await h.type('something else entirely')
    await h.key(ENTER)
    h.unmount()

    expect(onAnswer).toHaveBeenCalledWith('something else entirely')
  })

  it('names a quick-pick range that covers every row it draws', () => {
    const frame = frameOf(<ClarifyPrompt onAnswer={noop} onCancel={noop} req={req()} t={DEFAULT_THEME} />)

    // Two choices plus Other is three rows, so the hint has to say 1-3.
    expect(frame).toContain('1-3 quick pick')
  })

  it('ignores a number past the last row', async () => {
    const onAnswer = vi.fn()
    const h = driven(onAnswer)

    await h.key('9')
    h.unmount()

    expect(onAnswer).not.toHaveBeenCalled()
  })
})
