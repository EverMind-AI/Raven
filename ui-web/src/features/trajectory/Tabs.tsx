/* The tab strip of the details pane: the overview first, then one tab per
 * block, in the order the descriptor lists them -- the same order the
 * overview's sections have, so a heading there and a tab here are the one
 * thing twice.
 *
 * A tab that the descriptor marks as not available still has its place:
 * a required block with nothing behind it says why on its own tab rather
 * than disappearing, and the dot after its name says so at a glance. The
 * strip scrolls sideways when the tabs outrun it; it never wraps and never
 * squeezes a name into something unreadable.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

import { t } from '../../i18n/t'
import { INTERNAL_BLOCKS, blockTitleKey } from './blocks'
import * as details from './detailStore'

import type { TrajectoryBlockDescriptor } from './types'
import type { JSX, KeyboardEvent as ReactKeyboardEvent } from 'react'

/** How much of the strip one arrow press scrolls: most of what is in view. */
export const ARROW_STEP = 0.8

export const tabId = (tab: details.Tab): string => `trajectory-tab-${tab}`
export const panelId = (tab: details.Tab): string => `trajectory-panel-${tab}`

/** The word on a block's tab: the catalogue's, or the id when there is none. */
export function blockTitle(block: Pick<TrajectoryBlockDescriptor, 'id'>): string {
  const key = blockTitleKey(block.id)
  return key ? t(key) : block.id
}

export function Tabs({ entryId, blocks: all }: { entryId: string; blocks: TrajectoryBlockDescriptor[] }): JSX.Element {
  const current = details.tabOf(entryId)
  const blocks = all.filter((b) => !INTERNAL_BLOCKS.includes(b.id))
  const tabs: details.Tab[] = ['overview', ...blocks.map((b) => b.id)]
  const strip = useRef<HTMLDivElement | null>(null)
  const [reach, setReach] = useState({ left: false, right: false })

  /* Whether the strip has more than it shows on either side, re-read after
     every scroll and whenever the strip or its contents change size. */
  const measure = useCallback((): void => {
    const el = strip.current
    if (!el) return
    const over = el.scrollWidth > el.clientWidth + 1
    setReach({ left: over && el.scrollLeft > 0, right: over && el.scrollLeft + el.clientWidth < el.scrollWidth - 1 })
  }, [])
  useLayoutEffect(measure, [measure, tabs.length])
  useEffect(() => {
    const el = strip.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [measure])
  /* The selected tab is kept in view, however it was selected. */
  useLayoutEffect(() => {
    strip.current?.querySelector<HTMLElement>(`#${tabId(current)}`)?.scrollIntoView?.({ inline: 'nearest', block: 'nearest' })
    measure()
  }, [current, measure])
  const nudge = (direction: -1 | 1): void => {
    const el = strip.current
    if (!el) return
    el.scrollLeft += direction * el.clientWidth * ARROW_STEP
    measure()
  }

  /* Arrows move the selection and the focus together; Home and End go to the ends. */
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    const at = tabs.indexOf(current)
    let next: number | null = null
    if (e.key === 'ArrowRight') next = Math.min(tabs.length - 1, at + 1)
    else if (e.key === 'ArrowLeft') next = Math.max(0, at - 1)
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = tabs.length - 1
    if (next === null || next === at) return
    e.preventDefault()
    const tab = tabs[next]!
    details.setTab(entryId, tab)
    const el = e.currentTarget.querySelector<HTMLElement>(`#${tabId(tab)}`)
    el?.focus()
  }

  return (
    <div className="trajectory-tabs-wrap" data-reach-left={reach.left ? '' : undefined} data-reach-right={reach.right ? '' : undefined}>
      {reach.left || reach.right ? (
        <button className="trajectory-tab-arrow" aria-label={t('gui.trajectory.details.tabs_earlier')} disabled={!reach.left} onClick={() => nudge(-1)}>
          {'\u2039'}
        </button>
      ) : null}
    <div className="trajectory-tabs" role="tablist" aria-label={t('gui.trajectory.details.tabs')} ref={strip} onScroll={measure} onKeyDown={onKeyDown}>
      {tabs.map((tab) => {
        const block = tab === 'overview' ? null : blocks.find((b) => b.id === tab)
        const selected = tab === current
        const muted = block !== null && block !== undefined && block.availability !== 'available'
        return (
          <button
            key={tab}
            className={selected ? 'trajectory-tab trajectory-tab-on' : 'trajectory-tab'}
            id={tabId(tab)}
            role="tab"
            aria-selected={selected}
            aria-controls={panelId(tab)}
            tabIndex={selected ? 0 : -1}
            onClick={() => details.setTab(entryId, tab)}
          >
            {tab === 'overview' ? t('gui.trajectory.details.overview') : blockTitle(block!)}
            {muted ? <span className="trajectory-tab-dot" aria-hidden="true" /> : null}
          </button>
        )
      })}
    </div>
      {reach.left || reach.right ? (
        <button className="trajectory-tab-arrow" aria-label={t('gui.trajectory.details.tabs_later')} disabled={!reach.right} onClick={() => nudge(1)}>
          {'\u203a'}
        </button>
      ) : null}
    </div>
  )
}
