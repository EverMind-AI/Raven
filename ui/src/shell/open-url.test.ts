// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'

import { open } from './open-url'

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ''
  delete window.RavenShell
})

describe('the host URL action', () => {
  it('opens an HTTP URL without giving the new page an opener', () => {
    const launch = vi.spyOn(window, 'open').mockReturnValue(null)
    open('https://example.com/docs')
    expect(launch).toHaveBeenCalledWith('https://example.com/docs', '_blank', 'noopener')
  })

  it('copies a non-URL value and reports success', async () => {
    document.body.innerHTML = '<div id="toasts"></div>'
    window.RavenShell = { T: () => 'copied', toast: () => {}, menuAt: () => {}, confirmAsk: () => {}, showPage: () => {} }
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    open('/tmp/report.txt')
    await Promise.resolve()
    expect(writeText).toHaveBeenCalledWith('/tmp/report.txt')
    expect(document.querySelector('.toast .t')?.textContent).toBe('copied')
  })

  it('shows the original value when copying fails', async () => {
    document.body.innerHTML = '<div id="toasts"></div>'
    window.RavenShell = { T: () => 'copied', toast: () => {}, menuAt: () => {}, confirmAsk: () => {}, showPage: () => {} }
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: async () => Promise.reject(new Error('denied')) }, configurable: true,
    })
    open('search words')
    await new Promise((resolve) => window.setTimeout(resolve, 0))
    expect(document.querySelector('.toast .t')?.textContent).toBe('search words')
  })
})
