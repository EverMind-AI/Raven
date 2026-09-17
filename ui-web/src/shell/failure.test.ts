// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Shell } from './bridge'
import { _resetForTests, bootError, show } from './failure'
import { resetShell, setShell } from './bridge'
import { mountPageRoot } from '../test/pageRoot'

/* Both bars are drawn by src/chrome/FailureBar.tsx, so the page's own root has
   to be standing for one to reach the body (see src/main.tsx). */
let unmount = (): void => {}

function wire(): void {
  const shell: Shell = {
    T: (key, vars) => key === 'gui.boot_fail' ? `${vars?.where}:${vars?.err}` : key,
    confirmAsk: () => {},
    showPage: () => {},
  }
  setShell(shell)
}

beforeEach(() => {
  unmount = mountPageRoot()
})

afterEach(() => {
  /* A bar is never taken down in the page, so the store outlives a case. */
  _resetForTests()
  unmount()
  resetShell()
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('the failure bar writer', () => {
  it('appends the legacy top bar and updates the same node', () => {
    const bar = show('Checking')
    expect(document.body.innerHTML).toBe('<div class="topfail">Checking</div>')
    bar.say('Stopped')
    expect(document.body.innerHTML).toBe('<div class="topfail">Stopped</div>')
  })

  it('draws the boot fallback with its source line and reports the failure', () => {
    wire()
    const report = vi.spyOn(console, 'error').mockImplementation(() => {})
    const error = new Error('broken')
    error.stack = 'Error: broken\n    at boot.js:7:3'
    bootError('drawList', error)
    const bar = document.body.firstElementChild as HTMLElement
    expect(bar.className).toBe('')
    expect(bar.getAttribute('style')).toBe(
      'position: fixed; left: 0px; right: 0px; top: 0px; z-index: 99; background: #d96a5b; color: #fff; font: 12px / 1.5 ui-monospace, monospace; padding: 8px 14px; white-space: pre-wrap;'
    )
    expect(bar.textContent).toBe('drawList:broken\nat boot.js:7:3')
    expect(report).toHaveBeenCalledWith('[boot]', 'drawList', error)
  })
})
