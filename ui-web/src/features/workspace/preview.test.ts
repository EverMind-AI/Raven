// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'

import { openPreviewTab, previewRequestOf, previewURL } from './preview'

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

  it('opens the page in a tab, and a host that refuses the call is not an error', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    openPreviewTab('/repo/a.md', 's1')
    expect(open).toHaveBeenCalledWith(previewURL('/repo/a.md', 's1'), '_blank', 'noopener')
    open.mockImplementation(() => { throw new Error('blocked') })
    expect(() => openPreviewTab('/repo/a.md', null)).not.toThrow()
    open.mockRestore()
  })
})
