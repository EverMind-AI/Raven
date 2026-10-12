// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { useSyncExternalStore } from 'react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import * as details from './detailStore'
import { ARROW_STEP, Tabs } from './Tabs'

import type { TrajectoryBlockDescriptor } from './types'
import type { JSX } from 'react'

/* The pane redraws the strip on every change of the details store; alone in a test, the strip needs the same. */
function Host({ blocks }: { blocks: TrajectoryBlockDescriptor[] }): JSX.Element {
  useSyncExternalStore(details.subscribe, details.get)
  return <Tabs entryId="r0" blocks={blocks} />
}

const block = (id: string): TrajectoryBlockDescriptor => ({
  id, renderer: 'text', availability: 'available', preview: null, total_items: null, related_operation: null, reason: null,
})

const q = (sel: string): HTMLElement | null => document.querySelector<HTMLElement>(sel)

/* happy-dom lays nothing out: the strip's geometry is written by hand. */
function geometry(strip: HTMLElement, scrollWidth: number, clientWidth: number): void {
  Object.defineProperty(strip, 'scrollWidth', { get: () => scrollWidth, configurable: true })
  Object.defineProperty(strip, 'clientWidth', { get: () => clientWidth, configurable: true })
}

beforeEach(() => {
  details._resetForTests()
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  document.body.innerHTML = ''
})

afterEach(() => {
  cleanup()
  resetTranslator()
})

describe('the tab strip', () => {
  it('leaves the outline out of the tabs it offers', () => {
    render(<Tabs entryId="r0" blocks={[block('messages'), block('outline'), block('model')]} />)
    expect([...document.querySelectorAll('[role="tab"]')].map((t) => t.id)).toEqual(['trajectory-tab-overview', 'trajectory-tab-messages', 'trajectory-tab-model'])
  })

  it('shows no arrows while every tab fits, and both once they outrun the strip, each enabled for the side it can still move to', () => {
    render(<Tabs entryId="r0" blocks={Array.from({ length: 12 }, (_, i) => block(`b${i}`))} />)
    expect(q('.trajectory-tab-arrow')).toBeNull()
    const strip = q('.trajectory-tabs') as HTMLElement
    geometry(strip, 900, 300)
    act(() => { fireEvent.scroll(strip) })
    const arrows = [...document.querySelectorAll<HTMLButtonElement>('.trajectory-tab-arrow')]
    expect(arrows).toHaveLength(2)
    expect(arrows[0]!.disabled).toBe(true)
    expect(arrows[1]!.disabled).toBe(false)
    act(() => { fireEvent.click(arrows[1]!) })
    expect(strip.scrollLeft).toBe(300 * ARROW_STEP)
    const after = [...document.querySelectorAll<HTMLButtonElement>('.trajectory-tab-arrow')]
    expect(after[0]!.disabled).toBe(false)
    expect(after[1]!.disabled).toBe(false)
    strip.scrollLeft = 600
    act(() => { fireEvent.scroll(strip) })
    expect([...document.querySelectorAll<HTMLButtonElement>('.trajectory-tab-arrow')].map((a) => a.disabled)).toEqual([false, true])
    act(() => { fireEvent.click(q('.trajectory-tab-arrow') as HTMLElement) })
    expect(strip.scrollLeft).toBe(600 - 300 * ARROW_STEP)
  })

  it('keeps the selected tab in view when the keyboard moves it', () => {
    render(<Host blocks={Array.from({ length: 12 }, (_, i) => block(`b${i}`))} />)
    const strip = q('.trajectory-tabs') as HTMLElement
    const scrolled: string[] = []
    for (const tab of document.querySelectorAll<HTMLElement>('[role="tab"]')) tab.scrollIntoView = () => { scrolled.push(tab.id) }
    act(() => { fireEvent.keyDown(strip, { key: 'End' }) })
    expect(details.tabOf('r0')).toBe('b11')
    expect(scrolled.at(-1)).toBe('trajectory-tab-b11')
  })
})
