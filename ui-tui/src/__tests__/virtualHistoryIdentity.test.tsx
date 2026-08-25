// SPDX-License-Identifier: MIT
// Modifications Copyright (c) 2026 EverMind.

import { render } from 'ink-testing-library'
import React from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import type { ScrollBoxHandle } from '../types/hermes-ink.js'

import { useVirtualHistory } from '../hooks/useVirtualHistory.js'

// useMainApp feeds this hook's return value straight into the appTranscript
// memo, so a fresh object on every render invalidates the memo and re-renders
// the whole transcript on each keystroke. Identity has to survive a render
// that changed nothing the hook depends on.
let results: unknown[] = []

function IdentitySpy({ items }: { items: readonly { key: string }[] }) {
  const scrollRef = React.useRef<ScrollBoxHandle | null>(null)

  results.push(useVirtualHistory(scrollRef, items, 80))

  return null
}

describe('useVirtualHistory return identity', () => {
  beforeEach(() => {
    results = []
  })

  it('returns the same object when a re-render changes none of its inputs', () => {
    const items = [{ key: 'a' }, { key: 'b' }]
    const { rerender } = render(React.createElement(IdentitySpy, { items }))

    const settled = results.length

    rerender(React.createElement(IdentitySpy, { items }))

    expect(results.length).toBeGreaterThan(settled)
    expect(results.at(-1)).toBe(results[settled - 1])
  })

  it('returns a new object once the item list changes', () => {
    const items = [{ key: 'a' }]
    const { rerender } = render(React.createElement(IdentitySpy, { items }))

    const settled = results.length

    rerender(React.createElement(IdentitySpy, { items: [{ key: 'a' }, { key: 'b' }] }))

    expect(results.at(-1)).not.toBe(results[settled - 1])
  })
})
