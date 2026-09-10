// @vitest-environment happy-dom
/* A key field is masked, readable on request, and still a plain input to its caller. */

import { cleanup, fireEvent, render } from '@testing-library/react'
import { createRef } from 'react'
import { afterEach, describe, expect, it } from 'vitest'

import { KeyInput } from './key-input'

import type { Shell } from './bridge'

const shell: Shell = { T: (key: string) => key, confirmAsk: () => {}, showPage: () => {} }

afterEach(() => {
  cleanup()
  delete window.RavenShell
})

describe('the key field', () => {
  const mount = (): { input: HTMLInputElement; eye: HTMLButtonElement } => {
    window.RavenShell = shell
    const view = render(<KeyInput placeholder="API Key" />)
    return {
      input: view.container.querySelector('input')!,
      eye: view.container.querySelector('button.peek')!,
    }
  }

  it('starts masked and reads back on request', () => {
    const { input, eye } = mount()

    /* A credential should not be on screen for anyone walking past; a field
       that can never be read cannot be checked before pressing Connect. */
    expect(input.type).toBe('password')
    expect(eye.getAttribute('aria-pressed')).toBe('false')

    fireEvent.click(eye)
    expect(input.type).toBe('text')
    expect(eye.getAttribute('aria-pressed')).toBe('true')
    expect(eye.getAttribute('aria-label')).toBe('gui.key.hide')

    fireEvent.click(eye)
    expect(input.type).toBe('password')
  })

  it('keeps what was typed across a toggle', () => {
    const { input, eye } = mount()
    fireEvent.change(input, { target: { value: 'sk-live-123' } })

    fireEvent.click(eye)
    expect(input.value).toBe('sk-live-123')
    fireEvent.click(eye)
    expect(input.value).toBe('sk-live-123')
  })

  it('is still a plain input to whoever holds its ref', () => {
    /* Every caller hands it a ref and reads `.value` off it, the way it did
       when this was a bare `<input type="password">`. */
    window.RavenShell = shell
    const ref = createRef<HTMLInputElement>()
    render(<KeyInput ref={ref} aria-label="Anthropic API Key" />)

    expect(ref.current).toBeInstanceOf(HTMLInputElement)
    ref.current!.value = ' sk-x '
    expect(ref.current!.value.trim()).toBe('sk-x')
    expect(ref.current!.getAttribute('aria-label')).toBe('Anthropic API Key')
    /* No autofill offering a saved password into a provider credential box. */
    expect(ref.current!.getAttribute('autocomplete')).toBe('off')
  })

  it('stays out of the tab order between the field and its save button', () => {
    const { eye } = mount()
    expect(eye.tabIndex).toBe(-1)
  })
})
