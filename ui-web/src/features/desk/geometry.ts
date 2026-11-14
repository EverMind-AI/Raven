/** Geometry and width policy for the floating workspace desk. */

import type { DeskGeometry, DeskPane } from './types'

/* Bumped whenever the default placement or size changes. A stored geometry is
   written on the first render, so every reader who has opened the desk once
   already has the old numbers under the old key -- keeping the key would leave
   the new default reaching nobody but a fresh browser. */
export const DESK_GEOMETRY_KEY = 'raven.gui.desk.geometry.v7'
export const DESK_ANCHOR_GAP = 12
export const DESK_SNAP_DISTANCE = 34
/* How far the pointer travels before a press on the handle becomes a drag.
   The handle is mostly tab buttons -- measured on the running page, 182 of its
   298px -- so a press there has to be able to become either, and this is where
   the two part. Small enough that a deliberate drag is never mistaken for a
   click, large enough that a click's own jitter is never mistaken for a drag. */
export const DESK_DRAG_THRESHOLD = 4
export const DESK_MIN_SIZE = 250
/* The width only: the height has no number of its own -- how far down it
   reaches is the composer card's to say, however tall the window
   (`deskBottomLimit`). */
export const DESK_MAX_WIDTH = 480
export const DESK_VIEWPORT_GUTTER = 8
/* How far above the composer card the anchored desk stops when the card runs
   under its column (`deskBottomLimit`). */
export const DESK_COMPOSER_GAP = 12
/* The default is the smallest desk that shows a tab's contents, not the
   smallest one that renders -- it used to be 250x260 against a 250 minimum,
   which left a five-file shelf scrolling: 210px of body for 265px of rows.
   Width stays the cautious axis: the desk hangs UNDER the capsule and keeps
   its right edge there, so widening it reaches further back across the centred
   transcript column, which is what put the default at the minimum in the first
   place. Anchored below rather than beside, it covers the top of that column
   rather than the middle of it, and this much buys a tab that fits.
   The height is only a fallback: the default desk reaches the bottom
   (`DeskGeometry.fill`), and takes this where there is no limit to reach --
   no anchor drawn -- or once it is dragged loose. */
export const DESK_DEFAULT_WIDTH = 300
export const DESK_DEFAULT_HEIGHT = 340

/* The inset of what the palette hangs from -- the header's capsule, which holds
   the launcher -- from the chat's right edge, and the gap left between the
   panel and the text so the two do not touch. The first MUST agree with
   `--desk-launcher-edge` in the stylesheet, which is what actually places the
   panel -- the same pairing `anchoredGeometry` keeps with `[data-anchored]`.
   16 is the header's own right padding: the capsule ends there, the
   conversation menu at its end, and the panel's right edge lines up with it. */
export const DESK_LAUNCHER_EDGE = 16
export const DESK_TEXT_GAP = 12
/* The narrowest transcript the reserve will leave behind. Below it the reserve
   is not taken at all -- see `deskReserve`, where measuring showed that taking
   PART of it is worse than taking none. */
export const DESK_COLUMN_FLOOR = 520

/* How much of the chat's width the anchored panel is asking for.
 *
 * The transcript column is centred in the chat and the anchored panel hangs into
 * it. Measured on the running page in a 900px-tall window with a 300px panel at
 * the 330px it opened at before it opened full height, as the intersection of
 * the two rectangles:
 *
 *     1920px   nothing
 *     1600px   13px wide, 330px tall
 *     1440px   93px wide, 330px tall
 *     1280px  173px wide, 330px tall
 *
 * 330px is the panel's own height, so what it covers is a BAND ACROSS THE TOP of
 * the column -- which is where a short conversation's whole content sits, and
 * where any conversation's does once the reader scrolls back up. At that height
 * it does not reach the composer: the panel ends at y=394 and the composer sits
 * near the bottom of the window, so the two intersect on the x-axis only.
 * Pulled all the way down -- where it now opens (`DeskGeometry.fill`) -- it ends
 * level with the composer card's bottom edge (`deskBottomLimit`): a strip down
 * the column's whole height rather than a band across its top, beside the card
 * because the reserve moves the composer as well, and short of it where the
 * reserve stands aside.
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
const FILE_PANE_MIN_WIDTH = 320
const AGENT_PANE_INITIAL_WIDTH = 440
const WORKSPACE_EDGE_GUTTER = 8
const COLUMN_GAP = 6
const COLUMN_TRANSITION_DELTA = WORKSPACE_EDGE_GUTTER + COLUMN_GAP

/* The lowest the desk's bottom edge goes: the composer card's own bottom edge,
   so a palette pulled all the way down ends level with the box the reader
   types in. Asked about a column the card runs under -- the anchored palette's,
   see `anchoredRoom` -- it is the gap above the card instead: the landing page
   and a chat too narrow for the reserve leave the card there, where its bottom
   edge would put the palette on the field and the send button. A dock that is
   not drawn -- an imported session has none -- leaves the window's gutter
   as the limit, the case `dockLift` reads the same way. */
export function deskBottomLimit(column?: { left: number; right: number }): number {
  const card = document.querySelector('.chat > .dock > .dock-in')
  if (!card?.getClientRects().length) return window.innerHeight - DESK_VIEWPORT_GUTTER
  const rect = card.getBoundingClientRect()
  const under = column && rect.left < column.right && rect.right > column.left
  return under ? rect.top - DESK_COMPOSER_GAP : rect.bottom
}

export function clampGeometry(value: DeskGeometry): DeskGeometry {
  const w = Math.max(DESK_MIN_SIZE, Math.min(DESK_MAX_WIDTH, value.w, window.innerWidth - 16))
  const limit = deskBottomLimit()
  /* The height is the room between the top edge and the limit. Anchored, the
     stylesheet holds that edge under the capsule. Detached, the edge is
     wherever the palette was put, so the room is measured from the window's
     top instead: measured from the palette's own top, a downward move would
     shrink the palette rather than stop it. */
  const top = value.detached ? DESK_VIEWPORT_GUTTER : anchoredBox(w)?.top ?? value.y
  const h = Math.max(DESK_MIN_SIZE, Math.min(value.h, limit - top))
  return {
    x: Math.max(DESK_VIEWPORT_GUTTER, Math.min(window.innerWidth - w - DESK_VIEWPORT_GUTTER, value.x)),
    y: Math.max(DESK_VIEWPORT_GUTTER, Math.min(limit - h, value.y)),
    w,
    h,
    detached: value.detached,
    ...(value.fill ? { fill: true } : {}),
  }
}

function launcher(): HTMLElement | null {
  /* The chip is rendered on every page and the stylesheet hides it -- with no
     desk at all, and behind a fullscreen pane, where `#wsBtn` is the way to
     the panel instead. So the first candidate is not always the one on
     screen, and picking it anyway reads `display: none` as an all-zero box at
     the window's top-left corner. A box with no extent is not drawn: let the
     fallback have its turn, and when neither is drawn the answer is "no
     anchor", which keeps the geometry the reader already has. */
  const candidates = [
    document.querySelector('.desk-follow-toggle'),
    document.getElementById('wsBtn'),
  ]
  for (const candidate of candidates) {
    if (!candidate) continue
    const rect = candidate.getBoundingClientRect()
    if (rect.width > 0 || rect.height > 0) return candidate as HTMLElement
  }
  return null
}

/* What the anchored desk hangs from: the header's capsule when the launcher
   sits in one (chrome/ChatTop.tsx), so the palette lines up with the whole
   group of controls rather than with the chip in its middle; the launcher
   itself anywhere else. */
function anchor(): HTMLElement | null {
  const button = launcher()
  return (button?.closest('.chrome-capsule') as HTMLElement | null) ?? button
}

/* Where the anchored desk sits: the gap below its anchor, right edge flush
   with it -- the placement `anchoredGeometry` describes. */
function anchoredBox(w: number): { top: number; left: number; right: number } | null {
  const rect = anchor()?.getBoundingClientRect()
  return rect ? { top: rect.bottom + DESK_ANCHOR_GAP, left: rect.right - w, right: rect.right } : null
}

/* How tall the anchored desk may be drawn at this width as the composer stands
   now: down to the limit, asked about its own column. Not part of
   clampGeometry, which settles the height the reader sets: a card that runs
   under the palette for a while takes only what is drawn, and gives it back
   when it moves away. */
export function anchoredRoom(w: number): number {
  const box = anchoredBox(w)
  return box ? deskBottomLimit(box) - box.top : Number.POSITIVE_INFINITY
}

export function defaultGeometry(): DeskGeometry {
  const w = Math.min(DESK_DEFAULT_WIDTH, window.innerWidth - 20)
  const h = Math.min(DESK_DEFAULT_HEIGHT, window.innerHeight - 82)
  return anchoredGeometry({ x: 0, y: 0, w, h, detached: false, fill: true })
}

/* Where the anchored desk actually sits, for the magnet to snap back to. It
   MUST agree with .desk-palette[data-anchored="true"] in the stylesheet, which
   is what positions it while it is attached: right edge flush with the
   capsule the launcher sits in, hanging below it. Two expressions of one placement, so a change to
   either is a change to both. */
export function anchoredGeometry(value: DeskGeometry): DeskGeometry {
  const box = anchoredBox(value.w)
  if (!box) {
    return clampGeometry({
      ...value,
      x: window.innerWidth - value.w - DESK_VIEWPORT_GUTTER,
      y: DESK_VIEWPORT_GUTTER,
      detached: false,
    })
  }
  return clampGeometry({
    ...value,
    x: box.left,
    y: box.top,
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
  splitWidth,
  rememberedWidth,
  firstPane,
}: {
  previousWidth: number
  previousColumns: 0 | 1 | 2
  nextColumns: 1 | 2
  availableWidth: number
  splitWidth: number
  /* The width the reader last dragged the column to, if ever. */
  rememberedWidth?: number | null
  firstPane?: DeskPane
}): number {
  let desired = previousWidth
  if (previousColumns === 0) {
    /* A task pane's own board is sized like the agent conversation's, not
       like a file: 440 is the width the board's own card grid was laid out
       against (two 196px columns plus their gap and padding), and opening
       one at 720 would spend that extra width on nothing the graph uses.
       A file opens at half of what the chat and the pane share: a fixed 960
       took the whole of a laptop's width and left the composer too narrow to
       write in, and half-and-half is the split that serves reading the file
       and writing about it at once. A width the reader dragged the column to
       outranks all of these: it is the one they chose, and reopening should
       not take it back. */
    desired = rememberedWidth ? rememberedWidth : firstPane?.kind === 'agent' || firstPane?.kind === 'agent-record' || firstPane?.kind === 'task'
      ? AGENT_PANE_INITIAL_WIDTH
      : Math.max(FILE_PANE_MIN_WIDTH, Math.round(splitWidth / 2))
  }
  else if (nextColumns > previousColumns) desired = previousWidth * 2 + COLUMN_TRANSITION_DELTA
  else if (nextColumns < previousColumns) desired = Math.max(CHAT_MIN_FALLBACK, (previousWidth - COLUMN_TRANSITION_DELTA) / 2)
  return Math.min(desired, availableWidth)
}
