/* The tab strip of the details pane: the overview first, then one tab per
 * block, in the order the descriptor lists them -- the same order the
 * overview's sections have, so a heading there and a tab here are the one
 * thing twice.
 *
 * A tab that the descriptor marks as not available still has its place:
 * a required block with nothing behind it says why on its own tab rather
 * than disappearing, and the dot after its name says so at a glance. The
 * strip wraps onto a second line when the tabs outrun it: no sideways
 * scrolling and no pair of arrows to reveal what a scroll would hide.
 */

import { Pin02Icon } from '@hugeicons/core-free-icons'
import { useLayoutEffect, useRef } from 'react'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { blockTitleKey } from './blocks'
import * as details from './detailStore'

import type { TrajectoryBlockDescriptor } from './types'
import type { JSX, KeyboardEvent as ReactKeyboardEvent } from 'react'

/* Brings a tab into its strip's view by scrolling the strip alone. The
   browser's own way scrolls every ancestor that can scroll as well, and on a
   page wider than the window that slides the whole page sideways. */
export function keepInStrip(strip: HTMLElement, tab: HTMLElement): void {
  const box = strip.getBoundingClientRect()
  const at = tab.getBoundingClientRect()
  if (at.left < box.left) strip.scrollLeft -= box.left - at.left
  else if (at.right > box.right) strip.scrollLeft += at.right - box.right
}

export const tabId = (tab: details.Tab): string => `trajectory-tab-${tab}`
export const panelId = (tab: details.Tab): string => `trajectory-panel-${tab}`

/** The word on a block's tab: the catalogue's, or the id when there is none. */
export function blockTitle(block: Pick<TrajectoryBlockDescriptor, 'id'>): string {
  const key = blockTitleKey(block.id)
  return key ? t(key) : block.id
}

export function Tabs({ entryId, blocks }: { entryId: string; blocks: TrajectoryBlockDescriptor[] }): JSX.Element {
  const current = details.tabOf(entryId)
  const tabs = details.visibleTabs(blocks)
  const pin = details.pinned()
  const strip = useRef<HTMLDivElement | null>(null)

  /* The selected tab is kept in view, however it was selected. */
  useLayoutEffect(() => {
    const el = strip.current
    const tab = el?.querySelector<HTMLElement>(`#${tabId(current)}`)
    if (el && tab) keepInStrip(el, tab)
  }, [current])

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
    <div className="trajectory-tabs-wrap">
    <div className="trajectory-tabs" role="tablist" aria-label={t('gui.trajectory.details.tabs')} ref={strip} onKeyDown={onKeyDown}>
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
            data-pinned={tab === pin ? '' : undefined}
            onClick={() => details.setTab(entryId, tab)}
          >
            {tab === 'overview' ? t('gui.trajectory.details.overview') : blockTitle(block!)}
            {muted ? <span className="trajectory-tab-dot" aria-hidden="true" /> : null}
            {/* Named rather than hidden, so the tab's own name says it is the pinned one. */}
            {tab === pin ? (
              <span className="trajectory-tab-pin" role="img" aria-label={t('gui.trajectory.details.pinned')} title={t('gui.trajectory.details.pinned')}>
                <Icon icon={Pin02Icon} size={11} />
              </span>
            ) : null}
          </button>
        )
      })}
    </div>
    </div>
  )
}
