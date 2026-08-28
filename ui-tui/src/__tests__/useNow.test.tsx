// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Two panels showing the same node's elapsed seconds must print the same
// number. They mount at different moments -- the strip when the run starts, the
// transcript card when the tool row scrolls into the episode -- and before the
// shared clock each carried its own second boundary, so one read 13s while the
// other read 14s.
//
// Asserted on what the hook returns rather than on a painted frame, and inside
// one root, which is where both panels live: separate ink roots throttle their
// output independently and would lag each other whatever the clock said.

import { Text } from '@hermes/ink'
import { render } from 'ink-testing-library'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { fmtDuration } from '../domain/messages.js'
import { useNow } from '../hooks/useNow.js'

const STARTED_AT = 1_000_000

describe('useNow', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(STARTED_AT)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('reads the same second in panels mounted at different moments', async () => {
    const seen = new Map<string, string>()
    const Elapsed = ({ tag }: { tag: string }) => {
      const label = fmtDuration(useNow() - STARTED_AT)

      seen.set(tag, label)

      return <Text>{label}</Text>
    }
    const Tree = ({ card }: { card: boolean }) => (
      <>
        <Elapsed tag="strip" />
        {card ? <Elapsed tag="card" /> : null}
      </>
    )

    const view = render(<Tree card={false} />)

    // The card joins mid-second, which is what used to give it its own boundary.
    await vi.advanceTimersByTimeAsync(400)
    view.rerender(<Tree card />)

    for (const step of [0, 700, 1_000, 1_300, 2_000]) {
      await vi.advanceTimersByTimeAsync(step)
      expect(seen.get('card')).toBe(seen.get('strip'))
    }

    expect(seen.get('strip')).toBe('5s')
    view.unmount()
  })

  it('installs no timer while inactive', () => {
    const Idle = () => <Text>{fmtDuration(useNow(false) - STARTED_AT)}</Text>
    const view = render(<Idle />)

    expect(vi.getTimerCount()).toBe(0)
    view.unmount()
  })
})
