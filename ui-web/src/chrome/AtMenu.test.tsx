// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as mentions from '../state/mentions'
import { At } from './AtMenu'

/* The "@" in the composer bar: what the conversation is pointed at.
 *
 * The store's own tests cover when a pick reaches the engine; these cover what
 * the menu offers and what the button says while it is shut.
 */

let bases: Array<{ id: string; name: string; documents: number }> = []

vi.mock('../rpc/gateway', () => ({
  gateway: () => ({
    call: async (method: string) => (method === 'knowledge.bases.list' ? { bases } : {}),
  }),
}))

/* The catalogue is read when the bases panel opens, so every case that opens it
   has to let that land before it reads the rows. */
async function openBases(): Promise<void> {
  await act(async () => {
    ;(document.getElementById('atBtn') as HTMLButtonElement).click()
  })
  await act(async () => {
    ;(screen.getByText('Knowledge bases') as HTMLElement).click()
  })
  await vi.waitFor(() => expect(mentions.get().bases).not.toBeNull())
}

beforeEach(() => {
  mentions._resetForTests()
  mentions.useSession(() => null)
  bases = [
    { id: 'kb-a', name: 'handbook', documents: 12 },
    { id: 'kb-b', name: 'runbook', documents: 3 },
  ]
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('the at menu', () => {
  it('draws nothing until it is opened', () => {
    render(<At />)

    expect(document.getElementById('atPop')?.getAttribute('data-open')).toBe('false')
    /* A closed popover has no rows, so nothing under it can take a click. */
    expect(document.querySelectorAll('#atPop .prow')).toHaveLength(0)
  })

  it('offers the folder and the bases, in that order', async () => {
    render(<At />)

    await act(async () => {
      ;(document.getElementById('atBtn') as HTMLButtonElement).click()
    })

    const rows = [...document.querySelectorAll('#atPop .prow .nm')].map((n) => n.textContent)
    /* The folder first: it is the one every conversation has whether or not
       anybody set it. */
    expect(rows).toEqual(['Folder', 'Knowledge bases'])
  })

  it('lists the bases with what is in them', async () => {
    render(<At />)
    await openBases()

    expect(screen.getByText('handbook')).toBeTruthy()
    expect(screen.getByText('12 documents')).toBeTruthy()
  })

  it('ticks a base and says so on the button', async () => {
    render(<At />)
    await openBases()

    await act(async () => {
      ;(screen.getByText('handbook') as HTMLElement).click()
    })

    const row = screen.getByText('handbook').closest('button')!
    expect(row.getAttribute('aria-checked')).toBe('true')
    /* The scope of a question is worth seeing while it is being typed, not
       only while it is being set. */
    expect(document.querySelector('.chrome-at-n')?.textContent).toBe('1')
    expect(document.getElementById('atBtn')?.classList.contains('chrome-at-set')).toBe(true)
  })

  it('unticks by pressing again', async () => {
    render(<At />)
    await openBases()
    await act(async () => {
      ;(screen.getByText('handbook') as HTMLElement).click()
    })

    await act(async () => {
      ;(screen.getByText('handbook') as HTMLElement).click()
    })

    expect(mentions.count()).toBe(0)
    expect(document.querySelector('.chrome-at-n')).toBeNull()
  })

  it('goes back to the two rows from the bases', async () => {
    render(<At />)
    await openBases()

    await act(async () => {
      ;(document.querySelector('.chrome-at-back') as HTMLButtonElement).click()
    })

    expect([...document.querySelectorAll('#atPop .prow .nm')].map((n) => n.textContent)).toEqual([
      'Folder',
      'Knowledge bases',
    ])
  })

  it('says a machine with no bases has none', async () => {
    bases = []
    render(<At />)
    await openBases()

    expect(screen.getByText('No knowledge bases yet')).toBeTruthy()
  })

  it('carries the count on the rows as well, for a menu that is shut on the panel', async () => {
    render(<At />)
    await openBases()
    await act(async () => {
      ;(screen.getByText('handbook') as HTMLElement).click()
    })

    await act(async () => {
      ;(document.querySelector('.chrome-at-back') as HTMLButtonElement).click()
    })

    const row = screen.getByText('Knowledge bases').closest('button')!
    expect(row.querySelector('.chrome-at-count')?.textContent).toBe('1')
  })
  it('shuts when a pointer lands outside it', async () => {
    /* The popover has no close button. A pointer outside is one way back out
       and Escape is the other, and both are registered per popover by id --
       state/globalListeners.ts and state/escapeOrder.ts -- so a new one that
       nobody adds to either list stays up until its own button is pressed
       again. */
    const { installGlobalListeners } = await import('../state/globalListeners')
    installGlobalListeners()
    render(<At />)
    await act(async () => {
      ;(document.getElementById('atBtn') as HTMLButtonElement).click()
    })
    expect(mentions.get().open).toBe(true)

    await act(async () => {
      document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    })

    expect(mentions.get().open).toBe(false)
  })

  it('stays up for a pointer inside it', async () => {
    const { installGlobalListeners } = await import('../state/globalListeners')
    installGlobalListeners()
    render(<At />)
    await act(async () => {
      ;(document.getElementById('atBtn') as HTMLButtonElement).click()
    })

    await act(async () => {
      document.getElementById('atPop')!.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))
    })

    expect(mentions.get().open).toBe(true)
  })
  it('offers the folder only while it can still be changed', async () => {
    /* A conversation takes its working directory at the create and cannot be
       moved afterwards, which is why the chip hides itself there. Offered and
       pressed, the row would open a popover the same state forces shut, under
       an anchor that is not on screen. */
    const wd = await import('../state/workdir')
    wd.set({ ...wd.get(), paint: { label: 'thesis', title: '/w/thesis', set: true, locked: true } })
    render(<At />)

    await act(async () => {
      ;(document.getElementById('atBtn') as HTMLButtonElement).click()
    })

    expect([...document.querySelectorAll('#atPop .prow .nm')].map((n) => n.textContent)).toEqual([
      'Knowledge bases',
    ])
  })

  it('says which folder on the row rather than only behind it', async () => {
    const wd = await import('../state/workdir')
    wd.set({ ...wd.get(), paint: { label: 'thesis', title: '/w/thesis', set: true, locked: false } })
    render(<At />)

    await act(async () => {
      ;(document.getElementById('atBtn') as HTMLButtonElement).click()
    })

    const row = screen.getByText('Folder').closest('button')!
    expect(row.querySelector('.chrome-at-count')?.textContent).toBe('thesis')
  })
})
