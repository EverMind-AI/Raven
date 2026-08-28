// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The direct-chat switch order and its cycling, which is what Ctrl+Left /
// Ctrl+Right walks and what the Live Agents Strip's instance layer draws from.

import { describe, expect, it } from 'vitest'

import type { InstanceRow } from '../rpc/generated.js'

import { cycleTarget, orderedTargets } from '../app/directChatStore.js'

const row = (over: Partial<InstanceRow> & Pick<InstanceRow, 'agent' | 'handle'>): InstanceRow => ({
  createdAtMs: 0,
  kind: 'cli',
  resumable: true,
  sessionKey: 's1',
  status: 'completed',
  updatedAtMs: 0,
  ...over
})

describe('orderedTargets', () => {
  it('always leads with the main conversation', () => {
    expect(orderedTargets([], null)).toEqual([null])
  })

  it('orders instances by when they first appeared, and keeps that order', () => {
    // Not by `updatedAtMs`: it bumps on every status change, and with several
    // instances answering at once the cycle would resort under the user.
    const first = row({ agent: 'A', createdAtMs: 1, handle: 'one', updatedAtMs: 9 })
    const second = row({ agent: 'B', createdAtMs: 2, handle: 'two', updatedAtMs: 1 })

    expect(orderedTargets([second, first], null)).toEqual([
      null,
      { agent: 'A', handle: 'one' },
      { agent: 'B', handle: 'two' }
    ])

    const bumped = { ...second, updatedAtMs: 99 }

    expect(orderedTargets([first, bumped], null).slice(1)).toEqual([
      { agent: 'A', handle: 'one' },
      { agent: 'B', handle: 'two' }
    ])
  })

  it('leaves dag-node rows out', () => {
    // Their handle is minted by the runner, and continuing one has no resume
    // story; a target there would offer a chat that cannot work.
    expect(orderedTargets([row({ agent: 'Coder', handle: 'run-1/build', kind: 'dag-node' })], null)).toEqual([null])
  })

  it('offers only resumable instances', () => {
    expect(orderedTargets([row({ agent: 'A', handle: 'h', resumable: false })], null)).toEqual([null])
    expect(orderedTargets([row({ agent: 'A', handle: 'h', resumable: undefined })], null)).toEqual([null])
  })

  it('keeps the active instance whatever its resumable flag says', () => {
    // The conversation on screen must always be a way out of itself, and a
    // just-created instance's refresh may not have landed yet.
    const active = { agent: 'A', handle: 'h' }

    expect(orderedTargets([row({ agent: 'A', handle: 'h', resumable: false })], active)).toEqual([null, active])
  })
})

describe('cycleTarget', () => {
  const targets = orderedTargets([row({ agent: 'A', createdAtMs: 1, handle: 'one' }), row({ agent: 'B', createdAtMs: 2, handle: 'two' })], null)

  it('steps right from main to the first instance', () => {
    expect(cycleTarget(targets, null, 1)).toEqual({ agent: 'A', handle: 'one' })
  })

  it('wraps left from main to the last instance', () => {
    expect(cycleTarget(targets, null, -1)).toEqual({ agent: 'B', handle: 'two' })
  })

  it('wraps right from the last instance back to main', () => {
    expect(cycleTarget(targets, { agent: 'B', handle: 'two' }, 1)).toBeNull()
  })

  it('cycles from main when the active target is unknown', () => {
    expect(cycleTarget(targets, { agent: 'gone', handle: 'gone' }, 1)).toEqual({ agent: 'A', handle: 'one' })
  })

  it('answers main for an empty list', () => {
    expect(cycleTarget([], null, 1)).toBeNull()
  })
})
