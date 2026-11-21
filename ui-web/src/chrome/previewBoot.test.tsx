// @vitest-environment happy-dom
/* The preview window boots one component and one page listener. What this
   holds is the listener: a fenced listing's copy control is delegated on the
   document, and the window draws listings -- without the install, the button
   is there and does nothing. */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { bootPreview } from './previewBoot'

const TEXT = '# Notes\n\n```ts\nconst a = 1\n```\n'

afterEach(() => {
  document.body.innerHTML = ''
  delete document.documentElement.dataset.preview
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the preview window boot', () => {
  it('wires the code-block copy control the window draws', async () => {
    const write = vi.fn(async () => {})
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: write } })
    vi.stubGlobal('fetch', vi.fn(async () => new Response(TEXT, { status: 200 })))
    bootPreview({ path: '/repo/notes.md', session: null })
    await vi.waitFor(() => {
      expect(document.querySelector('.cbcp')).not.toBeNull()
    })
    document.querySelector<HTMLElement>('.cbcp')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(write).toHaveBeenCalledWith('const a = 1')
  })
})
