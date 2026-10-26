/* The Scope Tab strip, and the line that says which sub-agent run a tab is.
 *
 * The strip sits over everything else in the trajectory view: the main
 * session's tab first and always there, then one tab per sub-agent run the
 * reader opened, in the order they were opened, each with its own way out.
 * It shows only once the conversation has a run to open or a tab is open,
 * so a conversation with no sub-agent keeps the view it always had. It
 * scrolls sideways when the tabs outrun it, the way the details pane's block
 * strip does, and the arrow keys walk it the way a tab list is walked.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import { formatDuration } from '../../lib/duration'
import * as scopes from './scopes'
import * as list from './store'
import { ARROW_STEP, keepInStrip } from './Tabs'

import type { JSX, KeyboardEvent as ReactKeyboardEvent } from 'react'

export function ScopeTabs(): JSX.Element | null {
  const s = useSyncExternalStore(list.subscribe, list.get)
  const strip = useRef<HTMLDivElement | null>(null)
  const [reach, setReach] = useState({ left: false, right: false })
  const tabs: Array<string | null> = [null, ...s.scopes.map((o) => o.id)]
  const current = tabs.indexOf(s.scope)
  const shown = Object.keys(s.doors).length > 0 || s.scopes.length > 0
  /* What each tab says, mark included, in the language on screen: when any of
     it changes the tabs change width while the strip's own box may not, which
     no size observer on the strip reports. */
  const labels = s.scopes.map((o) => {
    const state = scopes.scopeState(s, o.id)
    return `${scopes.scopeTitle(scopes.scopeDoor(s, o.id))}\u0000${scopes.scopeMark(state) ?? ''}`
  })
  const words = [t('gui.trajectory.scope.main'), ...labels].join('\u0001')

  /* Whether the strip has more than it shows on either side, re-read after
     every scroll, whenever the strip changes size and whenever its tabs say
     something else. */
  const measure = useCallback((): void => {
    const el = strip.current
    if (!el) return
    const over = el.scrollWidth > el.clientWidth + 1
    const left = over && el.scrollLeft > 0
    const right = over && el.scrollLeft + el.clientWidth < el.scrollWidth - 1
    setReach((was) => (was.left === left && was.right === right ? was : { left, right }))
  }, [])
  useLayoutEffect(measure, [measure, words, shown])
  useEffect(() => {
    const el = strip.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [measure, shown])
  const over = reach.left || reach.right
  /* The tab on screen is kept in view, however it was chosen -- and again
     when the arrows come or go, or a tab before it grows. */
  useLayoutEffect(() => {
    const el = strip.current
    const tab = el?.querySelector<HTMLElement>(`#${scopes.scopeTabId(current)}`)
    if (el && tab) keepInStrip(el, tab)
    measure()
  }, [current, over, words, measure])

  if (!shown) return null

  const nudge = (direction: -1 | 1): void => {
    const el = strip.current
    if (!el) return
    el.scrollLeft += direction * el.clientWidth * ARROW_STEP
    measure()
  }

  /* Arrows move the tab and the focus together; Home and End go to the ends. */
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    let next: number | null = null
    if (e.key === 'ArrowRight') next = Math.min(tabs.length - 1, current + 1)
    else if (e.key === 'ArrowLeft') next = Math.max(0, current - 1)
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = tabs.length - 1
    if (next === null) return
    e.preventDefault()
    if (next !== current) list.switchScope(tabs[next] ?? null)
    e.currentTarget.querySelector<HTMLElement>(`#${scopes.scopeTabId(next)}`)?.focus()
  }

  /* Closing from inside the strip keeps the reader in the strip, on whichever
     tab is current after the close; closing from anywhere else leaves the
     focus where it was. */
  const close = (id: string): void => {
    const inside = strip.current?.contains(document.activeElement) ?? false
    scopes.close(id)
    if (inside) scopes.focusTab()
  }

  const mainOn = current === 0
  return (
    <div className="trajectory-scope-wrap" data-reach-left={reach.left ? '' : undefined} data-reach-right={reach.right ? '' : undefined}>
      {over ? (
        <button className="trajectory-scope-arrow" aria-label={t('gui.trajectory.details.tabs_earlier')} disabled={!reach.left} onClick={() => nudge(-1)}>
          {'‹'}
        </button>
      ) : null}
      <div className="trajectory-scope-tabs" role="tablist" aria-label={t('gui.trajectory.scope.strip')} ref={strip} onScroll={measure} onKeyDown={onKeyDown}>
        <button
          className={mainOn ? 'trajectory-scope-tab trajectory-scope-tab-on' : 'trajectory-scope-tab'}
          id={scopes.scopeTabId(0)}
          role="tab"
          aria-selected={mainOn}
          tabIndex={mainOn ? 0 : -1}
          onClick={() => list.switchScope(null)}
        >
          <span className="trajectory-scope-title">{t('gui.trajectory.scope.main')}</span>
        </button>
        {s.scopes.map((o, k) => {
          const at = k + 1
          const on = at === current
          const door = scopes.scopeDoor(s, o.id)
          const state = scopes.scopeState(s, o.id)
          const mark = scopes.scopeMark(state)
          const title = scopes.scopeTitle(door)
          const closeLabel = t('gui.trajectory.scope.close_named', { title })
          return (
            <div className="trajectory-scope-item" role="presentation" key={o.id}>
              <button
                className={on ? 'trajectory-scope-tab trajectory-scope-tab-on' : 'trajectory-scope-tab'}
                id={scopes.scopeTabId(at)}
                role="tab"
                aria-selected={on}
                tabIndex={on ? 0 : -1}
                title={scopes.scopeTooltip(door, state)}
                onClick={() => list.switchScope(o.id)}
              >
                {mark ? <span className={`trajectory-scope-mark trajectory-scope-mark-${mark}`} role="img" aria-label={scopes.stateWord(state)} /> : null}
                <span className="trajectory-scope-title">{title}</span>
              </button>
              <button className="trajectory-scope-close" aria-label={closeLabel} title={closeLabel} tabIndex={on ? 0 : -1} onClick={() => close(o.id)}>
                {'×'}
              </button>
            </div>
          )
        })}
      </div>
      {over ? (
        <button className="trajectory-scope-arrow" aria-label={t('gui.trajectory.details.tabs_later')} disabled={!reach.right} onClick={() => nudge(1)}>
          {'›'}
        </button>
      ) : null}
    </div>
  )
}

/* What the sub-agent tab on screen is: the agent, the task, how the run
   stands and for how long, the turn that dispatched it, and the way back to
   the entry that opens it in the main session. */
export function ScopeInfo(): JSX.Element | null {
  const s = useSyncExternalStore(list.subscribe, list.get)
  if (s.scope === null) return null
  const scope = s.scope
  const door = scopes.scopeDoor(s, scope)
  const state = scopes.scopeState(s, scope)
  const meta = door?.meta ?? {}
  const agent = typeof meta.agent === 'string' && meta.agent ? meta.agent : null
  const label = typeof meta.label === 'string' && meta.label ? meta.label : null
  /* A run gone from the record is told in the colour of an unknown end. */
  const tone = state === 'gone' ? 'unknown' : state
  const locate = (): void => {
    if (scopes.locate(scope)) scopes.focusList()
  }
  return (
    <div className="trajectory-scope-info" role="group" aria-label={scopes.scopeTitle(door)}>
      {agent ? <span className="trajectory-scope-agent">{agent}</span> : null}
      {label ? <span className="trajectory-scope-task" title={label}>{label}</span> : null}
      <span className={`trajectory-scope-state trajectory-status-${tone}`}>{scopes.stateWord(state)}</span>
      {typeof door?.duration_ms === 'number' ? <span className="trajectory-scope-dur">{formatDuration(door.duration_ms)}</span> : null}
      {typeof door?.turn_number === 'number'
        ? <span className="trajectory-scope-turn">{t('gui.trajectory.scope.dispatched_in', { n: door.turn_number })}</span>
        : null}
      {scope in s.doors ? (
        <button className="trajectory-link trajectory-scope-locate" onClick={locate}>{t('gui.trajectory.scope.locate')}</button>
      ) : null}
    </div>
  )
}
