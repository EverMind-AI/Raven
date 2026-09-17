// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { _resetForTests, bootError, show } from './failureBar'
import { mountPageRoot } from '../test/pageRoot'
import { resetTranslator, setTranslator } from '../i18n/t'
import * as pageStore from './page'
import * as confirmStore from './confirm'

/* Both bars are drawn by src/chrome/FailureBar.tsx, so the page's own root has
   to be standing for one to reach the body (see src/main.tsx) -- and that root
   renders the page, so a bar is the body's LAST child rather than its only
   one. */
let unmount = (): void => {}

function wire(): void {
  setTranslator((key, vars) => key === 'gui.boot_fail' ? `${vars?.where}:${vars?.err}` : key)
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
}

beforeEach(() => {
  unmount = mountPageRoot()
})

afterEach(() => {
  /* A bar is never taken down in the page, so the store outlives a case. */
  _resetForTests()
  unmount()
  resetTranslator()
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('the failure bar writer', () => {
  it('appends the legacy top bar and updates the same node', () => {
    const bar = show('Checking')
    const node = document.body.lastElementChild as HTMLElement
    expect(node.outerHTML).toBe('<div class="topfail">Checking</div>')
    bar.say('Stopped')
    expect(document.body.lastElementChild).toBe(node)
    expect(node.outerHTML).toBe('<div class="topfail">Stopped</div>')
  })

  it('draws the boot fallback with its source line and reports the failure', () => {
    wire()
    const report = vi.spyOn(console, 'error').mockImplementation(() => {})
    const error = new Error('broken')
    error.stack = 'Error: broken\n    at boot.js:7:3'
    bootError('drawList', error)
    const bar = document.body.lastElementChild as HTMLElement
    expect(bar.className).toBe('')
    expect(bar.getAttribute('style')).toBe(
      'position: fixed; left: 0px; right: 0px; top: 0px; z-index: 99; background: #d96a5b; color: #fff; font: 12px / 1.5 ui-monospace, monospace; padding: 8px 14px; white-space: pre-wrap;'
    )
    expect(bar.textContent).toBe('drawList:broken\nat boot.js:7:3')
    expect(report).toHaveBeenCalledWith('[boot]', 'drawList', error)
  })
})
