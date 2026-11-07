// @vitest-environment happy-dom
/* A key field is masked, readable on request, and still a plain input to its caller. */

import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { createRef } from 'react'
import { afterEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import { KeyInput } from './KeyInput'


setTranslator((key: string) => key)

afterEach(() => {
  cleanup()
  resetTranslator()
})

describe('the key field', () => {
  const mount = (): { input: HTMLInputElement; eye: HTMLButtonElement } => {
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

  it('lets a caller that can fetch the saved key fill the field before the eye opens', async () => {
    /* Turned first and filled after, the field would flash empty and then
       jump; the caller is told what the eye turns to, and the turn waits. */
    const told: boolean[] = []
    let release = (): void => {}
    const view = render(<KeyInput onPeek={(shown) => { told.push(shown); return new Promise<void>((done) => { release = done }) }} />)
    const input = view.container.querySelector('input')!
    const eye = view.container.querySelector('button.peek')!

    fireEvent.click(eye)
    expect(told).toEqual([true])
    await Promise.resolve()
    expect(input.type).toBe('password')
    release()
    await waitFor(() => expect(input.type).toBe('text'))

    fireEvent.click(eye)
    expect(told).toEqual([true, false])
    release()
    await waitFor(() => expect(input.type).toBe('password'))
  })

  it('still turns when the caller could not fetch anything', async () => {
    const view = render(<KeyInput onPeek={() => Promise.reject(new Error('refused'))} />)
    fireEvent.click(view.container.querySelector('button.peek')!)
    await waitFor(() => expect(view.container.querySelector('input')!.type).toBe('text'))
  })
})
