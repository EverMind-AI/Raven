// @vitest-environment happy-dom
/* The shared clipboard action preserves the page's quiet failure policy. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { copy } from './clipboard'

function install(writeText?: (text: string) => Promise<void>): void {
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: writeText ? { writeText } : undefined,
  })
}

beforeEach(() => {
  document.body.innerHTML = '<div id="toasts"></div>'
})

afterEach(() => {
  install()
  vi.restoreAllMocks()
})

describe('the clipboard action', () => {
  it('writes text and speaks the success notice', async () => {
    const write = vi.fn(async () => {})
    install(write)
    copy('alpha', 'copied')
    await Promise.resolve()
    expect(write).toHaveBeenCalledWith('alpha')
    expect(document.querySelector('.toast .t')?.textContent).toBe('copied')
  })

  it('stays quiet when the host refuses the write', async () => {
    install(async () => { throw new Error('denied') })
    copy('alpha', 'copied')
    await Promise.resolve()
    expect(document.querySelector('.toast')).toBeNull()
  })

  it('does nothing when the clipboard API is absent', () => {
    install()
    expect(() => copy('alpha', 'copied')).not.toThrow()
    expect(document.querySelector('.toast')).toBeNull()
  })
})
