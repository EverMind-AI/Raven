// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { LiveAgentRow } from '../app/liveAgentsStore.js'

import { stripRowLabel, stripRows } from '../components/liveAgentsStrip.js'

const row = (over: Partial<LiveAgentRow>): LiveAgentRow => ({
  id: 't1',
  kind: 'spawn',
  label: 'find the bug',
  seq: 1,
  status: 'running',
  ...over
})

describe('stripRows', () => {
  it('shows active runs only, in insertion order', () => {
    const { overflow, visible } = stripRows([
      row({ id: 'a', status: 'running' }),
      row({ id: 'b', status: 'completed' }),
      row({ id: 'c', status: 'pending' }),
      row({ id: 'd', status: 'failed' })
    ])

    expect(visible.map(r => r.id)).toEqual(['a', 'c'])
    expect(overflow).toBe(0)
  })

  it('caps the strip and counts the rest as overflow', () => {
    const rows = ['a', 'b', 'c', 'd', 'e', 'f'].map(id => row({ id }))
    const { overflow, visible } = stripRows(rows, 4)

    expect(visible).toHaveLength(4)
    expect(overflow).toBe(2)
  })
})

describe('stripRowLabel', () => {
  it('pairs the agent with the run label', () => {
    expect(stripRowLabel(row({ agent: 'research-raven', label: 'jay chou research' }), 80)).toBe(
      'research-raven · jay chou research'
    )
  })

  it('prefers the instance handle for a spawn that has one', () => {
    expect(stripRowLabel(row({ agent: 'Raven-PPT', instance: 'jay-chou-ppt-6247a0' }), 80)).toBe(
      'Raven-PPT · jay-chou-ppt-6247a0'
    )
  })

  it('truncates to the given budget', () => {
    const label = stripRowLabel(row({ agent: 'a'.repeat(50), label: 'b'.repeat(50) }), 20)

    expect(label.length).toBeLessThanOrEqual(20)
    expect(label.endsWith('…')).toBe(true)
  })
})

describe('stripRows lingering', () => {
  const settled = (agoMs: number) => row({ settledAtMs: 1_000_000 - agoMs, status: 'completed' })

  it('drops settled rows by default, which is what makes the strip a live monitor', () => {
    expect(stripRows([settled(0), row({ id: 't2', seq: 2 })], 4, 1_000_000).visible).toHaveLength(1)
  })

  it('keeps a settled row inside the window when one is configured', () => {
    const { visible } = stripRows([settled(5_000)], 4, 1_000_000, 30_000)

    expect(visible.map(r => r.status)).toEqual(['completed'])
  })

  it('drops it again once the window has passed', () => {
    expect(stripRows([settled(45_000)], 4, 1_000_000, 30_000).visible).toEqual([])
  })

  it('drops a settled row carrying no local timestamp, which predates this session', () => {
    // `settledAtMs` is stamped when this client saw the row turn terminal; a row
    // seeded from disk has none and no window it could still be inside.
    expect(stripRows([row({ status: 'completed' })], 4, 1_000_000, 30_000).visible).toEqual([])
  })
})
