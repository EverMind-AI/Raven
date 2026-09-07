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

/* The launcher's own inset from the chat's right edge, and the gap left between
   the panel and the text so the two do not touch. The first MUST agree with
   `--desk-launcher-edge` in the stylesheet, which is what actually places the
   panel -- the same pairing `anchoredGeometry` keeps with `[data-anchored]`. */
export const DESK_LAUNCHER_EDGE = 12
export const DESK_TEXT_GAP = 12
/* The narrowest transcript the reserve will leave behind. Below it the reserve
   is not taken at all -- see `deskReserve`, where measuring showed that taking
   PART of it is worse than taking none. */
export const DESK_COLUMN_FLOOR = 520

/* How much of the chat's width the anchored panel is asking for.
 *
 * The transcript column is centred in the chat and the anchored panel hangs into
 * it. Measured on the running page in a 900px-tall window with the default 300px
 * panel, as the intersection of the two rectangles:
 *
 *     1920px   nothing
 *     1600px   13px wide, 330px tall
 *     1440px   93px wide, 330px tall
 *     1280px  173px wide, 330px tall
 *
 * 330px is the panel's own height, so what it covers is a BAND ACROSS THE TOP of
 * the column -- which is where a short conversation's whole content sits, and
 * where any conversation's does once the reader scrolls back up. It does not
 * reach the composer: the panel ends at y=394 and the composer sits near the
 * bottom of the window, so the two intersect on the x-axis only.
 *
 * So the chat gives the width up instead, and the column re-centres in what is
 * left. That is what already happens when the workspace column opens (`--wsw`),
 * which is why it is this shape rather than a nudge holding the column still.
 *
 * All of it or none of it, and the floor is why. Giving up as much as fits and
 * no more sounds gentler and measured WORSE: at 900px it narrowed the column to
 * the 520px floor and the panel still landed 190px into it, leaving 330px of
 * readable width where reserving nothing leaves 342px. So under the floor this
 * answers 0 and the panel overlaps exactly as it does today -- the reserve can
 * improve on that or stand aside, never undercut it.
 *
 * Zero while detached, too: a dragged panel is where the reader put it, and
 * holding a column of space for one floating over the middle of the window
 * would be reserving against a position it no longer has.
 */
export function deskReserve(desk: {
  shown: boolean
  /* Optional to match `DeskGeometry`, where absent means anchored -- the same
     reading `data-anchored={!geom.detached}` gives it in the markup. */
  detached?: boolean
  w: number
  chatWidth: number
}): number {
  if (!desk.shown || desk.detached) return 0
  const want = desk.w + DESK_LAUNCHER_EDGE + DESK_TEXT_GAP
  return desk.chatWidth - want >= DESK_COLUMN_FLOOR ? want : 0
}

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
