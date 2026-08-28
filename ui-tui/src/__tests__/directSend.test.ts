// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { beforeEach, describe, expect, it } from 'vitest'

import { bindDirectSender, canSendDirect, resetDirectSender, sendDirect } from '../app/directSend.js'

const target = { agent: 'A', handle: 'one' }

beforeEach(() => {
  resetDirectSender()
})

describe('directSend', () => {
  it('rejects before a sender is bound', async () => {
    expect(canSendDirect()).toBe(false)
    await expect(sendDirect(target, 'hi')).rejects.toThrow('not attached')
  })

  it('forwards to the bound sender', async () => {
    const sent: unknown[] = []
    bindDirectSender(async (t, c) => {
      sent.push([t, c])
    })

    await sendDirect(target, 'hi')

    expect(sent).toEqual([[target, 'hi']])
  })

  it('lets a stale unbind leave the current binding alone', async () => {
    const unbindFirst = bindDirectSender(async () => {})
    bindDirectSender(async () => {})

    unbindFirst()

    expect(canSendDirect()).toBe(true)
  })
})
