// SPDX-License-Identifier: MIT
// Modifications Copyright (c) 2026 EverMind.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it } from 'vitest'

import { TextInput } from '../components/textInput.js'

// SSH, tmux and a blocked event loop all coalesce keystrokes, so a burst of
// typed characters reaches stdin as one chunk. It has to land in the composer
// as typing, not as a paste held back by the paste debounce.
const flush = () => new Promise(resolve => setImmediate(resolve))

const mount = () => {
  const changes: string[] = []
  const stdin = new PassThrough()
  const stdout = new PassThrough()

  // Non-TTY stdout keeps fast echo out of the picture, so every accepted
  // character reaches onChange synchronously and the assertion needs no timer.
  Object.assign(stdout, { columns: 80, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  stdout.resume()

  const Harness = () => {
    const [value, setValue] = React.useState('')

    return React.createElement(TextInput, {
      focus: true,
      onChange: (next: string) => {
        changes.push(next)
        setValue(next)
      },
      value
    })
  }

  const instance = renderSync(React.createElement(Harness), {
    patchConsole: false,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

  return {
    changes,
    type: async (s: string) => {
      stdin.write(s)
      await flush()
    },
    unmount: () => {
      instance.unmount()
      instance.cleanup()
    }
  }
}

describe('typing burst reaching the composer', () => {
  it('accepts a coalesced run of characters without waiting on the paste debounce', async () => {
    const h = mount()

    try {
      await h.type('hello')

      expect(h.changes).toEqual(['h', 'he', 'hel', 'hell', 'hello'])
    } finally {
      h.unmount()
    }
  })

  it('accepts a coalesced run of wide characters', async () => {
    const h = mount()

    try {
      await h.type('你好')

      expect(h.changes).toEqual(['你', '你好'])
    } finally {
      h.unmount()
    }
  })
})
