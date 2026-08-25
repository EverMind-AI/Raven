// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { beforeEach, describe, expect, it } from 'vitest'

import type { TranscriptMessage } from '../rpc/index.js'

import {
  $dagNodeTraces,
  getDagNodeTrace,
  resetDagNodeTraces,
  setDagNodeTrace
} from '../app/dagNodeStore.js'

const say = (text: string): TranscriptMessage => ({ role: 'assistant', text })

beforeEach(() => {
  resetDagNodeTraces()
})

describe('dagNodeStore', () => {
  it('reads back what it stored', () => {
    setDagNodeTrace('r/a', [say('one')], false)

    expect(getDagNodeTrace('r/a')).toEqual({ failures: 0, messages: [say('one')], settled: false })
  })

  it('is undefined for a node never fetched', () => {
    expect(getDagNodeTrace('r/nope')).toBeUndefined()
  })

  it('publishes a new map so a subscriber re-renders', () => {
    const before = $dagNodeTraces.get()

    setDagNodeTrace('r/a', [say('one')], false)

    expect($dagNodeTraces.get()).not.toBe(before)
  })

  it('does not publish when the trace has not moved', () => {
    setDagNodeTrace('r/a', [say('one')], false)

    const after = $dagNodeTraces.get()

    setDagNodeTrace('r/a', [say('one')], false)

    // The poll re-reads twice a second and an idle agent returns the same
    // snapshot every time; republishing it would re-render every graph in the
    // transcript for no change.
    expect($dagNodeTraces.get()).toBe(after)
  })

  it('publishes when settled flips even though the messages did not', () => {
    setDagNodeTrace('r/a', [say('one')], false)

    const after = $dagNodeTraces.get()

    setDagNodeTrace('r/a', [say('one')], true)

    // A terminal status can land on a response whose messages are byte-identical
    // to the one before it -- settled has to lead the signature or this
    // transition would be dropped as "no change" and never recorded.
    expect($dagNodeTraces.get()).not.toBe(after)
    expect(getDagNodeTrace('r/a')).toEqual({ failures: 0, messages: [say('one')], settled: true })
  })

  it('publishes when the newest message grew', () => {
    setDagNodeTrace('r/a', [say('one')], false)

    const after = $dagNodeTraces.get()

    setDagNodeTrace('r/a', [say('one and a half')], false)

    expect($dagNodeTraces.get()).not.toBe(after)
  })

  it('publishes when a message was appended', () => {
    setDagNodeTrace('r/a', [say('one')], false)

    const after = $dagNodeTraces.get()

    setDagNodeTrace('r/a', [say('one'), say('two')], false)

    expect($dagNodeTraces.get()).not.toBe(after)
  })

  it('empties on reset', () => {
    setDagNodeTrace('r/a', [say('one')], false)
    resetDagNodeTraces()

    expect(getDagNodeTrace('r/a')).toBeUndefined()
  })

  it('publishes when a tool call gains the arguments it was announced without', () => {
    // The ACP transport announces a tool call before its arguments are known
    // (rawInput: {}) and sends the real input later via a tool_call_update frame.
    // The call count stays 1 the whole time, so it would be missed without
    // looking at each call's arguments.
    const withoutArgs = {
      role: 'assistant' as const,
      tool_calls: [{ id: 'c1', name: 'read_file', arguments: '{}' }]
    }
    const withArgs = {
      role: 'assistant' as const,
      tool_calls: [{ id: 'c1', name: 'read_file', arguments: '{"path":"a/b.ts"}' }]
    }

    setDagNodeTrace('r/a', [withoutArgs], false)

    const after = $dagNodeTraces.get()

    setDagNodeTrace('r/a', [withArgs], false)

    expect($dagNodeTraces.get()).not.toBe(after)
  })
})
