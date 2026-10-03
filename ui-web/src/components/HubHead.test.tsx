// @vitest-environment happy-dom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../i18n/t'
import * as hub from '../state/hub'
import { HubHead } from './HubHead'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

setTranslator((key) => key)

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const tabs = (): HTMLButtonElement[] => [...document.querySelectorAll<HTMLButtonElement>('.hub-head-tab')]

describe('the hub header', () => {
  it('names both modules and marks the one on screen', () => {
    render(<HubHead current="channels" />)
    expect(tabs().map((b) => b.textContent)).toEqual(['gui.hub.agents', 'gui.hub.channels'])
    expect(tabs().map((b) => b.getAttribute('aria-selected'))).toEqual(['false', 'true'])
  })

  /* The line under the tabs belongs to the module on screen, not the hub. */
  it('says what the module on screen is for', () => {
    const { rerender } = render(<HubHead current="agents" />)
    expect(document.querySelector('.hub-head-sub')!.textContent).toBe('gui.page.agents_sub')
    rerender(<HubHead current="channels" />)
    expect(document.querySelector('.hub-head-sub')!.textContent).toBe('gui.page.conn_sub')
  })

  /* The current tab is a title, not a control: pressing it would reopen the
     page it is on and re-fetch its rows for nothing. */
  it('opens the other module, and does nothing for the current one', () => {
    const open = vi.spyOn(hub, 'open').mockImplementation(() => {})
    render(<HubHead current="agents" />)
    act(() => tabs()[0]!.click())
    expect(open).not.toHaveBeenCalled()
    act(() => tabs()[1]!.click())
    expect(open).toHaveBeenCalledWith('channels')
  })
})
