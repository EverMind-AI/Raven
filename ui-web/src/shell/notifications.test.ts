// @vitest-environment happy-dom
// Verifies desktop notification preferences, permissions, and focus gating.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { enabled, load, setEnabled, show } from './notifications'

const shown: Array<{ title: string; body?: string }> = []

class FakeNotification {
  static permission: NotificationPermission = 'granted'

  constructor(title: string, options?: NotificationOptions) {
    shown.push({ title, body: options?.body })
  }
}

beforeEach(() => {
  localStorage.clear()
  shown.length = 0
  FakeNotification.permission = 'granted'
  Object.defineProperty(window, 'Notification', { configurable: true, writable: true, value: FakeNotification })
  setEnabled(false)
})

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('desktop notifications', () => {
  it('persists and reloads the front-end preference', () => {
    setEnabled(true)
    expect(enabled()).toBe(true)
    expect(JSON.parse(localStorage.getItem('raven.gui.ntf') || '{}')).toEqual({ on: true })
    localStorage.setItem('raven.gui.ntf', JSON.stringify({ on: false }))
    load()
    expect(enabled()).toBe(false)
  })

  it('speaks only while enabled, granted, and in the background', () => {
    setEnabled(true)
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    show('done', 'foreground')
    expect(shown).toEqual([])
    vi.mocked(document.hasFocus).mockReturnValue(false)
    show('done', 'background')
    expect(shown).toEqual([{ title: 'done', body: 'background' }])
  })

  it('allows the settings test button to force a foreground notice', () => {
    setEnabled(true)
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    show('test', '', { force: true })
    expect(shown).toEqual([{ title: 'test', body: undefined }])
  })

  it('refuses disabled and ungranted notices', () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    show('disabled')
    setEnabled(true)
    FakeNotification.permission = 'denied'
    show('denied')
    expect(shown).toEqual([])
  })
})
