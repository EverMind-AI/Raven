// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import { close, isOpen, open } from './lightbox'

import type { Shell } from './bridge'

function wire(): void {
  const shell: Shell = {
    T: (key) => key,
    toast: () => {},
    menuAt: () => {},
    confirmAsk: () => {},
    showPage: () => {},
  }
  window.RavenShell = shell
  document.body.innerHTML = ''
}

const overlay = (): HTMLElement | null => document.querySelector('.lightbox')

afterEach(() => {
  close()
  delete window.RavenShell
  document.body.innerHTML = ''
})

describe('the lightbox', () => {
  it('appends one overlay carrying the image, and reports itself open', () => {
    wire()
    expect(isOpen()).toBe(false)
    open('data:image/png;base64,AA', 'shot.png')
    const box = overlay()!
    expect(box.tagName).toBe('BUTTON')
    expect(box.getAttribute('aria-label')).toBe('gui.img.close')
    const img = box.querySelector('img')!
    expect(img.getAttribute('src')).toBe('data:image/png;base64,AA')
    expect(img.alt).toBe('shot.png')
    expect(isOpen()).toBe(true)
  })

  it('keeps a nameless image nameless rather than inventing alt text', () => {
    wire()
    open('data:image/png;base64,AA')
    expect(document.querySelector('.lightbox img')!.getAttribute('alt')).toBe('')
  })

  it('closes on a click anywhere on it', () => {
    wire()
    open('x')
    ;(overlay() as HTMLElement).click()
    expect(overlay()).toBeNull()
  })

  it('never stacks two, so one click cannot leave one behind', () => {
    wire()
    open('one')
    open('two')
    expect(document.querySelectorAll('.lightbox').length).toBe(1)
    expect(document.querySelector('.lightbox img')!.getAttribute('src')).toBe('two')
  })

  /* The chrome's Escape chain finds the overlay by class and calls close(); it
     never asks whether one is open, so close() has to be safe with none. */
  it('closes nothing without complaining', () => {
    wire()
    expect(() => close()).not.toThrow()
    expect(isOpen()).toBe(false)
  })
})
