// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { useSyncExternalStore } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import * as details from './detailStore'
import * as list from './store'
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
/* A box where happy-dom lays nothing out: its left edge and its width. */
const rect = (left: number, width: number): DOMRect =>
  ({ left, right: left + width, top: 0, bottom: 30, width, height: 30, x: left, y: 0, toJSON: () => ({}) }) as DOMRect

function geometry(strip: HTMLElement, scrollWidth: number, clientWidth: number): void {
  Object.defineProperty(strip, 'scrollWidth', { get: () => scrollWidth, configurable: true })
  Object.defineProperty(strip, 'clientWidth', { get: () => clientWidth, configurable: true })
}

beforeEach(() => {
  list._resetForTests()
  details._resetForTests()
  list.sessionChanged('gui:a')
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

  it('keeps the selected tab in view when the keyboard moves it, by scrolling the strip and nothing else', () => {
    render(<Host blocks={Array.from({ length: 12 }, (_, i) => block(`b${i}`))} />)
    const strip = q('.trajectory-tabs') as HTMLElement
    const intoView = vi.fn()
    for (const tab of document.querySelectorAll<HTMLElement>('[role="tab"]')) tab.scrollIntoView = intoView
    strip.getBoundingClientRect = () => rect(0, 300)
    ;(q('#trajectory-tab-b11') as HTMLElement).getBoundingClientRect = () => rect(820, 60)
    act(() => { fireEvent.keyDown(strip, { key: 'End' }) })
    expect(details.tabOf('r0')).toBe('b11')
    expect(strip.scrollLeft).toBe(580)
    ;(q('#trajectory-tab-overview') as HTMLElement).getBoundingClientRect = () => rect(-580, 70)
    act(() => { fireEvent.keyDown(strip, { key: 'Home' }) })
    expect(strip.scrollLeft).toBe(0)
    expect(intoView).not.toHaveBeenCalled()
  })

  it('brings the selected tab into view again once the arrows take their room, and not on a nudge', () => {
    details.setTab('r0', 'b11')
    render(<Host blocks={Array.from({ length: 12 }, (_, i) => block(`b${i}`))} />)
    const strip = q('.trajectory-tabs') as HTMLElement
    /* With the arrows in, the strip is narrower and the selected tab ends 20px past its edge. */
    strip.getBoundingClientRect = () => rect(22, 256)
    ;(q('#trajectory-tab-b11') as HTMLElement).getBoundingClientRect = () => rect(238, 60)
    geometry(strip, 900, 300)
    act(() => { fireEvent.scroll(strip) })
    expect(document.querySelectorAll('.trajectory-tab-arrow')).toHaveLength(2)
    expect(strip.scrollLeft).toBe(20)
    strip.scrollLeft = 0
    act(() => { fireEvent.scroll(strip) })
    /* A nudge changes which arrows are enabled, not whether they show: the
       reader's own scroll stands, and the selected tab, still past the edge,
       is not brought back over it. */
    const arrows = (): HTMLButtonElement[] => [...document.querySelectorAll<HTMLButtonElement>('.trajectory-tab-arrow')]
    expect(arrows().map((a) => a.disabled)).toEqual([true, false])
    act(() => { fireEvent.click(arrows()[1]!) })
    act(() => { fireEvent.scroll(strip) })
    expect(strip.scrollLeft).toBe(300 * ARROW_STEP)
    expect(arrows().map((a) => a.disabled)).toEqual([false, false])
  })
})

describe('the pinned mark', () => {
  const pinnedTabs = (): string[] => [...document.querySelectorAll('[data-pinned]')].map((t) => t.id)

  it('marks only the pinned tab, by name, wherever the selection moves', () => {
    render(<Host blocks={[block('a'), block('b'), block('c')]} />)
    expect(q('.trajectory-tab-pin')).toBeNull()
    act(() => { details.setTab('r0', 'b'); details.togglePin('r0') })
    const marks = [...document.querySelectorAll('.trajectory-tab-pin')]
    expect(marks.map((m) => m.closest('[role="tab"]')!.id)).toEqual(['trajectory-tab-b'])
    expect(marks[0]!.getAttribute('role')).toBe('img')
    expect(marks[0]!.getAttribute('aria-label')).toBe('gui.trajectory.details.pinned')
    expect(pinnedTabs()).toEqual(['trajectory-tab-b'])
    act(() => { details.setTab('r0', 'c') })
    expect(pinnedTabs()).toEqual(['trajectory-tab-b'])
  })

  it('marks nothing when the pinned tab is not among the blocks', () => {
    details.setTab('r0', 'z')
    details.togglePin('r0')
    render(<Host blocks={[block('a'), block('b')]} />)
    expect(details.pinned()).toBe('z')
    expect(q('.trajectory-tab-pin')).toBeNull()
    expect(pinnedTabs()).toEqual([])
  })
})
