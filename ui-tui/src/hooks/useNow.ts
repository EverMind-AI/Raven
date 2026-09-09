// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { useEffect, useState } from 'react'

import { sharedNow, subscribeSharedNow } from '../lib/uiClock.js'

/**
 * Re-render once a second while `active`, against the clock every other
 * elapsed-seconds label uses (see `lib/uiClock`). Idle callers install no timer.
 */
export const useNow = (active = true): number => {
  const [now, setNow] = useState(sharedNow)

  useEffect(() => {
    if (!active) {
      return
    }

    setNow(sharedNow())

    return subscribeSharedNow(setNow)
  }, [active])

  return now
}
