// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

/* What the approval prompt does once its own deadline has passed.
 *
 * Zero seconds is not a shorter deadline, it IS the deadline. The runtime's
 * ceiling sits a few seconds past it so a choice made BEFORE it survives
 * event-loop and RPC lag -- transport tolerance, not five more seconds of
 * authority to grant. `ApprovalBroker.resolve` cannot tell a late-arriving
 * early choice from a fresh one made after the deadline (measured: an `allow`
 * sent after `visible_timeout_s` is accepted and the call runs), so the prompt
 * is the party that has to stop offering.
 *
 * It still answers nothing when it expires -- that is the other half, and
 * approvalRoundTrip.test.ts holds it. Here: it also stops accepting.
 *
 * Driven through a raw-mode PassThrough because ink only routes keystrokes to
 * `useInput` when stdin looks like a TTY, which ink-testing-library's stub is
 * not. Same harness the clarify-prompt typing tests use.
 */

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it, vi } from 'vitest'

import type { ApprovalReq } from '../types.js'

import { ApprovalPrompt } from '../components/prompts.js'
import { DEFAULT_THEME } from '../theme.js'

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))
const noop = () => {}

const ENTER = '\r'
const TAB = '\t'

const driven = (onChoice: (c: string, f?: string, id?: string) => void, expiresAt: number) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: noop, setRawMode: noop, unref: noop })
  Object.assign(stderr, { isTTY: true })

  const req: ApprovalReq = {
    approvalId: 'a-1',
    command: 'rm notes.md',
    conversationId: 'tui:1',
    description: 'Approve this action',
    expiresAt
  }

  const instance = renderSync(<ApprovalPrompt onChoice={onChoice} req={req} t={DEFAULT_THEME} />, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  return {
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

describe('an approval prompt past its deadline', () => {
  it('accepts a choice while there is still time', async () => {
    // The half that must keep working. Without it, "accepts nothing" would pass
    // against a prompt that had stopped accepting anything at all.
    const onChoice = vi.fn()
    const ui = driven(onChoice, Date.now() + 30_000)

    try {
      await ui.key('1')
      expect(onChoice).toHaveBeenCalledWith('allow', '', 'a-1')
    } finally {
      ui.unmount()
    }
  })

  it('accepts no quick pick once the deadline has passed', async () => {
    const onChoice = vi.fn()
    const ui = driven(onChoice, Date.now() - 1_000)

    try {
      await ui.key('1')
      await ui.key(ENTER)
      expect(onChoice).not.toHaveBeenCalled()
    } finally {
      ui.unmount()
    }
  })

  it('accepts no note either, which submits through ink and not through useInput', async () => {
    // `TextInput` handles Enter itself, so the guard on `useInput` never sees
    // it. Its own guard is what this covers.
    const onChoice = vi.fn()
    const live = driven(onChoice, Date.now() + 30_000)

    try {
      // Open the note on Deny, which is where the selection already sits.
      await live.key(TAB)
      await live.type('not in prod')
      await live.key(ENTER)
      expect(onChoice).toHaveBeenCalledWith('deny', 'not in prod', 'a-1')
    } finally {
      live.unmount()
    }

    // The note has to be OPEN when the deadline passes, which is the only way
    // this path is reachable: `useInput`'s own guard stops Tab from opening one
    // after expiry, so a test that opens it late exercises nothing.
    const after = vi.fn()
    const dying = driven(after, Date.now() + 700)

    try {
      await dying.key(TAB)
      await dying.type('not in prod')
      await delay(900)
      await dying.key(ENTER)
      expect(after).not.toHaveBeenCalled()
    } finally {
      dying.unmount()
    }
  })
})
