/**
 * Pins the alternate-screen exit: one leave however many teardown paths ask
 * for it, and no repaint onto the primary screen once shutdown has begun.
 */
import { PassThrough } from 'node:stream'

import { describe, expect, it, vi } from 'vitest'

import { AlternateScreen } from './components/AlternateScreen.js'
import Text from './components/Text.js'
import instances from './instances.js'
import { renderSync } from './root.js'
import { EXIT_ALT_SCREEN } from './termio/dec.js'

const count = (text: string, part: string): number => text.split(part).length - 1

describe('alternate-screen exit', () => {
  it('leaves once even when shutdown and React cleanup both ask', () => {
    const stdout = new PassThrough()
    const stdin = new PassThrough()
    Object.assign(stdout, { columns: 80, fd: 1, isTTY: true, rows: 24 })
    Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })

    const instance = renderSync(
      <Text>hello</Text>,
      { patchConsole: false, stderr: stdout as never, stdin: stdin as never, stdout: stdout as never }
    )
    const owner = instances.get(stdout)
    expect(owner).toBeDefined()
    owner!.setAltScreenActive(true, true)
    const writes: string[] = []

    try {
      expect(owner!.leaveAltScreen(write => writes.push(write), false)).toBe(true)
      expect(owner!.leaveAltScreen(write => writes.push(write), false)).toBe(false)
    } finally {
      Object.assign(stdout, { isTTY: false })
      instance.unmount()
      instance.cleanup()
    }

    expect(count(writes.join(''), EXIT_ALT_SCREEN)).toBe(1)
  })

  it('routes both React cleanup and instance shutdown through the same guard', () => {
    const stdout = new PassThrough()
    const stdin = new PassThrough()

    Object.assign(stdout, { columns: 80, fd: 1, isTTY: true, rows: 24 })
    Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })

    const instance = renderSync(<Text>main</Text>, {
      patchConsole: false,
      stderr: stdout as never,
      stdin: stdin as never,
      stdout: stdout as never
    })
    const owner = instances.get(stdout)
    const original = instances.get(process.stdout)
    expect(owner).toBeDefined()
    instances.set(process.stdout, owner as never)

    try {
      const leave = vi.spyOn(owner!, 'leaveAltScreen')
      instance.rerender(
        <AlternateScreen>
          <Text>fullscreen</Text>
        </AlternateScreen>
      )
      expect(owner!.isAltScreenActive).toBe(true)
      instance.rerender(<Text>main</Text>)
      expect(leave).toHaveBeenCalledTimes(1)

      instance.rerender(
        <AlternateScreen>
          <Text>fullscreen</Text>
        </AlternateScreen>
      )
      expect(owner!.isAltScreenActive).toBe(true)
      instance.unmount()
      expect(leave).toHaveBeenCalledTimes(3)
    } finally {
      if (original) {
        instances.set(process.stdout, original)
      } else {
        instances.delete(process.stdout)
      }
      Object.assign(stdout, { isTTY: false })
      instance.cleanup()
    }
  })
})
