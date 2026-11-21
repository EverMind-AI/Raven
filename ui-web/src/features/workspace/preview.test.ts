// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'

import { openPreviewWindow, previewName, previewRequestOf, previewURL } from './preview'

describe('the file preview address', () => {
  it('answers a request only for a preview address that names a file', () => {
    expect(previewRequestOf('?preview=1&path=%2Frepo%2Fnotes.md&session=s1'))
      .toEqual({ path: '/repo/notes.md', session: 's1' })
    expect(previewRequestOf('?preview=1&path=%2Frepo%2Fnotes.md'))
      .toEqual({ path: '/repo/notes.md', session: null })
    expect(previewRequestOf('?stub=1')).toBeNull()
    expect(previewRequestOf('?preview=1')).toBeNull()
  })

  it('builds the window address from this page and keeps the session with it', () => {
    expect(previewURL('/repo/a.md', 's1'))
      .toBe(`${location.pathname}?preview=1&path=%2Frepo%2Fa.md&session=s1`)
    expect(previewURL('/repo/a.md', null))
      .toBe(`${location.pathname}?preview=1&path=%2Frepo%2Fa.md`)
  })

  it('reuses one window per file, and says when a blocker refused it', () => {
    expect(previewName('/repo/a.md')).toBe('raven-file:/repo/a.md')
    const open = vi.spyOn(window, 'open')
    open.mockReturnValue(null)
    expect(openPreviewWindow('/repo/a.md', null)).toBe(false)
    expect(open).toHaveBeenCalledWith(
      previewURL('/repo/a.md', null),
      'raven-file:/repo/a.md',
      'popup,width=1040,height=800',
    )
    open.mockReturnValue({} as Window)
    expect(openPreviewWindow('/repo/a.md', null)).toBe(true)
    open.mockRestore()
  })
})
