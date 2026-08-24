/** Geometry and width policy for the floating workspace desk. */

import type { DeskGeometry, DeskPane } from './deskTypes'

export const DESK_GEOMETRY_KEY = 'raven.gui.desk.geometry.v4'
export const DESK_ANCHOR_GAP = 12
export const DESK_ANCHOR_TOP = 4
export const DESK_SNAP_DISTANCE = 34
export const DESK_MIN_SIZE = 250
export const DESK_MAX_SIZE = 480
export const DESK_VIEWPORT_GUTTER = 8

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
  const w = Math.min(300, window.innerWidth - 20)
  const h = Math.min(300, window.innerHeight - 82)
  const anchor = document.getElementById('wsBtn')?.getBoundingClientRect()
  return clampGeometry({
    x: anchor ? anchor.left - w - DESK_ANCHOR_GAP : window.innerWidth - w - 42,
    y: anchor ? anchor.top - DESK_ANCHOR_TOP : DESK_VIEWPORT_GUTTER,
    w,
    h,
    detached: false,
  })
}

export function anchoredGeometry(value: DeskGeometry): DeskGeometry {
  const rect = launcher()?.getBoundingClientRect()
  if (!rect) return clampGeometry(value)
  return clampGeometry({
    ...value,
    x: rect.left - value.w - DESK_ANCHOR_GAP,
    y: rect.top - DESK_ANCHOR_TOP,
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

export function workspaceColumnCount(paneCount: number): 0 | 1 | 2 {
  if (paneCount <= 0) return 0
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
