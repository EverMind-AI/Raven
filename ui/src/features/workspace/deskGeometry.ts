/** Geometry and width policy for the floating workspace desk. */

import type { DeskGeometry, DeskPane } from './deskTypes'

/* Bumped whenever the default placement or size changes. A stored geometry is
   written on the first render, so every reader who has opened the desk once
   already has the old numbers under the old key -- keeping the key would leave
   the new default reaching nobody but a fresh browser. */
export const DESK_GEOMETRY_KEY = 'raven.gui.desk.geometry.v6'
export const DESK_ANCHOR_GAP = 12
export const DESK_SNAP_DISTANCE = 34
export const DESK_MIN_SIZE = 250
export const DESK_MAX_SIZE = 480
export const DESK_VIEWPORT_GUTTER = 8
/* The default is the smallest desk that shows a tab's contents, not the
   smallest one that renders -- it used to be 250x260 against a 250 minimum,
   which left a five-file shelf scrolling: 210px of body for 265px of rows.
   Width stays the cautious axis: the desk hangs UNDER the launcher and keeps
   its right edge there, so widening it reaches further back across the centred
   transcript column, which is what put the default at the minimum in the first
   place. Anchored below rather than beside, it covers the top of that column
   rather than the middle of it, and this much buys a tab that fits. */
export const DESK_DEFAULT_WIDTH = 300
export const DESK_DEFAULT_HEIGHT = 340

const CHAT_MIN_FALLBACK = 430
const FILE_PANE_INITIAL_WIDTH = 720
const AGENT_PANE_INITIAL_WIDTH = 440
const WORKSPACE_EDGE_GUTTER = 8
const COLUMN_GAP = 6
const COLUMN_TRANSITION_DELTA = WORKSPACE_EDGE_GUTTER + COLUMN_GAP

export function clampGeometry(value: DeskGeometry): DeskGeometry {
  const w = Math.max(DESK_MIN_SIZE, Math.min(DESK_MAX_SIZE, value.w, window.innerWidth - 16))
  const h = Math.max(DESK_MIN_SIZE, Math.min(DESK_MAX_SIZE, value.h, window.innerHeight - 16))
  return {
    x: Math.max(DESK_VIEWPORT_GUTTER, Math.min(window.innerWidth - w - DESK_VIEWPORT_GUTTER, value.x)),
    y: Math.max(DESK_VIEWPORT_GUTTER, Math.min(window.innerHeight - h - DESK_VIEWPORT_GUTTER, value.y)),
    w,
    h,
    detached: value.detached,
  }
}

function launcher(): HTMLElement | null {
  return (document.querySelector('.desk-follow-toggle') as HTMLElement | null)
    ?? document.getElementById('wsBtn')
}

export function defaultGeometry(): DeskGeometry {
  const w = Math.min(DESK_DEFAULT_WIDTH, window.innerWidth - 20)
  const h = Math.min(DESK_DEFAULT_HEIGHT, window.innerHeight - 82)
  return anchoredGeometry({ x: 0, y: 0, w, h, detached: false })
}

/* Where the anchored desk actually sits, for the magnet to snap back to. It
   MUST agree with .desk-palette[data-anchored="true"] in the stylesheet, which
   is what positions it while it is attached: right edge flush with the
   launcher, hanging below it. Two expressions of one placement, so a change to
   either is a change to both. */
export function anchoredGeometry(value: DeskGeometry): DeskGeometry {
  const rect = launcher()?.getBoundingClientRect()
  if (!rect) {
    return clampGeometry({
      ...value,
      x: window.innerWidth - value.w - DESK_VIEWPORT_GUTTER,
      y: DESK_VIEWPORT_GUTTER,
      detached: false,
    })
  }
  return clampGeometry({
    ...value,
    x: rect.right - value.w,
    y: rect.bottom + DESK_ANCHOR_GAP,
    detached: false,
  })
}

export function magnetGeometry(value: DeskGeometry): DeskGeometry {
  const loose = clampGeometry({ ...value, detached: true })
  const target = anchoredGeometry(loose)
  return Math.abs(loose.x - target.x) <= DESK_SNAP_DISTANCE
    && Math.abs(loose.y - target.y) <= DESK_SNAP_DISTANCE
    ? target
    : loose
}

export function workspaceColumnCount(paneCount: number, duo: 'rows' | 'cols' = 'rows'): 0 | 1 | 2 {
  if (paneCount <= 0) return 0
  /* Two panes side by side spend width the way three panes do, not the way a
     stack does: the workspace has to widen for them or each gets half of a
     column that was sized for one. */
  if (paneCount === 2 && duo === 'cols') return 2
  return paneCount >= 3 ? 2 : 1
}

export function workspaceAvailableWidth(splitWidth: number, viewportWidth: number, chatMin = CHAT_MIN_FALLBACK): number {
  return viewportWidth <= 1040 ? splitWidth : Math.max(0, splitWidth - chatMin)
}

export function workspaceTransitionWidth({
  previousWidth,
  previousColumns,
  nextColumns,
  availableWidth,
  firstPane,
}: {
  previousWidth: number
  previousColumns: 0 | 1 | 2
  nextColumns: 1 | 2
  availableWidth: number
  firstPane?: DeskPane
}): number {
  let desired = previousWidth
  if (previousColumns === 0) {
    desired = firstPane?.kind === 'agent' || firstPane?.kind === 'agent-record'
      ? AGENT_PANE_INITIAL_WIDTH
      : FILE_PANE_INITIAL_WIDTH
  }
  else if (nextColumns > previousColumns) desired = previousWidth * 2 + COLUMN_TRANSITION_DELTA
  else if (nextColumns < previousColumns) desired = Math.max(CHAT_MIN_FALLBACK, (previousWidth - COLUMN_TRANSITION_DELTA) / 2)
  return Math.min(desired, availableWidth)
}
