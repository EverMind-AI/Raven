// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'

import { close, install, show } from './menu'

afterEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('the menu writer', () => {
  it('draws actions and separators in order, preserving the dangerous class', () => {
    document.body.innerHTML = '<div class="menu" id="menu"></div>'
    show(20, 30, [{ label: 'Open', fn: () => {} }, '-', { label: 'Delete', bad: true, fn: () => {} }])
    expect(document.getElementById('menu')!.innerHTML).toBe(
      '<button>Open</button><hr><button class="bad">Delete</button>'
    )
    expect(document.getElementById('menu')!.dataset.open).toBe('true')
  })

  it('clamps the lower edge to the viewport and invokes one picked action', () => {
    document.body.innerHTML = '<div id="menu"></div>'
    const menu = document.getElementById('menu')!
    vi.spyOn(menu, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      width: 80,
      height: 60,
      top: 0,
      right: 80,
      bottom: 60,
      left: 0,
      toJSON: () => ({})
    })
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 200 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 150 })
    const calls: string[] = []
    show(190, 140, [{ label: 'Pick', fn: () => calls.push('pick') }])
    expect(menu.style.left).toBe('112px')
    expect(menu.style.top).toBe('82px')
    ;(menu.querySelector('button') as HTMLButtonElement).click()
    expect(calls).toEqual(['pick'])
    expect(menu.dataset.open).toBe('false')
  })

  it('closes the menu that owns an action if the standing host is replaced', () => {
    document.body.innerHTML = '<div id="menu"></div>'
    const first = document.getElementById('menu')!
    show(0, 0, [{ label: 'Pick', fn: () => {} }])
    const button = first.querySelector('button') as HTMLButtonElement
    first.remove()
    document.body.innerHTML = '<div id="menu" data-open="true"></div>'
    button.click()
    expect(first.dataset.open).toBe('false')
    expect(document.getElementById('menu')!.dataset.open).toBe('true')
  })

  it('closes on an outside pointer and stays open for a pointer inside', () => {
    document.body.innerHTML =
      '<div id="menu" data-open="true"><button>Here</button></div><button id="away">Away</button>'
    install()
    document.querySelector('#menu button')!.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    expect(document.getElementById('menu')!.dataset.open).toBe('true')
    document.getElementById('away')!.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    expect(document.getElementById('menu')!.dataset.open).toBe('false')
  })

  it('closes explicitly and tolerates a page without the menu host', () => {
    expect(() => close()).not.toThrow()
    expect(() => show(0, 0, [])).not.toThrow()
  })
})
