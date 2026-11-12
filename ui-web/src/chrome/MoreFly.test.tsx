// @vitest-environment happy-dom
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as imported from '../features/imported/store'
import * as navfly from '../state/navfly'
import { MoreFly } from './MoreFly'

/* The More group of the rail: the row that opens it, and the panel it opens.
 *
 * What the group holds is the import row, and what the import domain does with
 * a picked file is that domain's own case. What is here is the group's own
 * behaviour: how it opens, how it is shut, and what it does while a package is
 * on its way -- the row that names the file is inside the panel, so the panel
 * has to stay open for as long as the name is there.
 */

beforeEach(() => {
  navfly._resetForTests()
  imported._resetForTests()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const more = (): HTMLButtonElement => document.getElementById('navMoreBtn') as HTMLButtonElement
const panel = (): HTMLElement => document.getElementById('chrome-navfly') as HTMLElement

describe('the More group', () => {
  it('is shut when the page is served, with the import row inside the panel', () => {
    render(<MoreFly />)

    expect(panel().getAttribute('data-open')).toBe('false')
    expect(more().getAttribute('aria-expanded')).toBe('false')
    expect(panel().querySelector('#importedBtn')?.textContent).toBe('Import session')
  })

  it('opens from its own row and shuts when that row is pressed again', () => {
    render(<MoreFly />)

    act(() => more().click())
    expect(panel().getAttribute('data-open')).toBe('true')
    expect(more().getAttribute('aria-expanded')).toBe('true')

    act(() => more().click())
    expect(panel().getAttribute('data-open')).toBe('false')
    expect(more().getAttribute('aria-expanded')).toBe('false')
  })

  it('announces one action as a dialog, not a menu', () => {
    render(<MoreFly />)

    expect(more().getAttribute('aria-haspopup')).toBe('true')
    expect(more().getAttribute('aria-controls')).toBe('chrome-navfly')
    expect(panel().getAttribute('role')).toBe('dialog')
  })

  it('hands focus back to its row when it shuts from inside the panel', () => {
    render(<MoreFly />)
    act(() => more().click())
    const row = document.getElementById('importedBtn') as HTMLButtonElement
    act(() => row.focus())
    expect(document.activeElement).toBe(row)

    act(() => navfly.close())

    expect(document.activeElement).toBe(more())
  })

  it('shuts when a pointer lands outside it', async () => {
    /* The popover has no close button: a pointer outside is one way back out
       and Escape is the other (state/escapeOrder.ts), so a group that nobody
       adds to either list stays up until its own row is pressed again. */
    const { installGlobalListeners } = await import('../state/globalListeners')
    installGlobalListeners()
    render(<MoreFly />)
    act(() => more().click())
    expect(navfly.isOpen()).toBe(true)

    act(() => {
      document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    })

    expect(navfly.isOpen()).toBe(false)
  })

  it('stays up for a pointer inside it', async () => {
    const { installGlobalListeners } = await import('../state/globalListeners')
    installGlobalListeners()
    render(<MoreFly />)
    act(() => more().click())

    act(() => {
      panel().dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    })

    expect(navfly.isOpen()).toBe(true)
  })

  it('stands open while a package is on its way, and cannot be folded away from it', () => {
    render(<MoreFly />)
    act(() => {
      imported.set({ busy: 'trip.tar.gz' })
    })

    /* The file's name is in the popover, so the popover is what the reader
       looks at to see it. */
    expect(panel().getAttribute('data-open')).toBe('true')
    expect(panel().querySelector('#importedBtn')?.textContent).toContain('trip.tar.gz')

    act(() => more().click())
    expect(panel().getAttribute('data-open')).toBe('true')
  })

  it('picks the file from the row inside the panel', () => {
    const pick = vi.spyOn(imported, 'pick').mockImplementation(() => {})
    render(<MoreFly />)

    act(() => {
      ;(document.getElementById('importedBtn') as HTMLButtonElement).click()
    })

    expect(pick).toHaveBeenCalledTimes(1)
    pick.mockRestore()
  })
})
