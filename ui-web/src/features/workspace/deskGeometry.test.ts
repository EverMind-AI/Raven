/** Tests for floating workspace geometry and column transitions. */

// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import {
  anchoredGeometry,
  DESK_ANCHOR_GAP,
  DESK_COLUMN_FLOOR,
  DESK_LAUNCHER_EDGE,
  DESK_TEXT_GAP,
  deskReserve,
  DESK_DEFAULT_HEIGHT,
  DESK_DEFAULT_WIDTH,
  DESK_MAX_SIZE,
  DESK_MIN_SIZE,
  defaultGeometry,
  workspaceColumnCount,
  workspaceTransitionWidth,
} from './deskGeometry'

/* The launcher, at the size and place the stylesheet gives it: a 30px button
   centred on y=27 against the right edge with a 12px gutter. */
const EDGE = 12

function launcher(): number {
  const right = window.innerWidth - EDGE
  const button = document.createElement('button')
  button.className = 'desk-follow-toggle'
  document.body.appendChild(button)
  button.getBoundingClientRect = () => ({
    left: right - 30, right, top: 12, bottom: 42,
    width: 30, height: 30, x: right - 30, y: 12, toJSON: () => ({}),
  }) as DOMRect
  return right
}

afterEach(() => { document.body.innerHTML = '' })

describe('desk placement', () => {
  it('hangs the desk under the launcher, flush with it, clear of the chat column', () => {
    const right = launcher()
    const at = defaultGeometry()
    expect(at.w).toBe(DESK_DEFAULT_WIDTH)
    expect(at.h).toBe(DESK_DEFAULT_HEIGHT)
    /* Right edges level, and below the button rather than beside it -- beside
       it, the desk reached back over the centred transcript. */
    expect(at.x + at.w).toBe(right)
    expect(at.y).toBe(42 + DESK_ANCHOR_GAP)
    expect(at.detached).toBe(false)
  })

  /* Not the minimum. The desk opens at a size a reader can read a tab in, and
     the minimum is only what they are allowed to shrink it to -- the two were
     the same number and the shelf came up showing three of however many rows
     it held. */
  it('opens larger than it can be shrunk to, on both axes', () => {
    expect(DESK_DEFAULT_WIDTH).toBeGreaterThan(DESK_MIN_SIZE)
    expect(DESK_DEFAULT_HEIGHT).toBeGreaterThan(DESK_MIN_SIZE)
    expect(DESK_DEFAULT_WIDTH).toBeLessThanOrEqual(DESK_MAX_SIZE)
    expect(DESK_DEFAULT_HEIGHT).toBeLessThanOrEqual(DESK_MAX_SIZE)
  })

  it('snaps a dragged desk back to that same place', () => {
    const right = launcher()
    const back = anchoredGeometry({ x: 300, y: 400, w: 260, h: 300, detached: true })
    expect(back.x + back.w).toBe(right)
    expect(back.y).toBe(42 + DESK_ANCHOR_GAP)
    expect(back.detached).toBe(false)
  })
})

describe('workspace geometry', () => {
  it('uses one column for two panes and two columns from the third pane', () => {
    expect(workspaceColumnCount(0)).toBe(0)
    expect(workspaceColumnCount(2)).toBe(1)
    /* A pair turned sideways spends width like three panes, not like a stack. */
    expect(workspaceColumnCount(2, 'cols')).toBe(2)
    expect(workspaceColumnCount(3)).toBe(2)
  })

  it('expands into a second column without shrinking the existing column', () => {
    expect(workspaceTransitionWidth({
      previousWidth: 520,
      previousColumns: 1,
      nextColumns: 2,
      availableWidth: 1200,
    })).toBe(1054)
  })

  it('clamps a second column to the available viewport width', () => {
    expect(workspaceTransitionWidth({
      previousWidth: 720,
      previousColumns: 1,
      nextColumns: 2,
      availableWidth: 970,
    })).toBe(970)
  })
})

/* What the chat gives up so the anchored panel is not sitting on the transcript.
 *
 * The cases that matter are the two that answer NOTHING, because both are a
 * choice: a dragged panel is where the reader put it, and a chat too narrow to
 * spare the width keeps it. The second is not a rounding-down -- taking part of
 * the reserve measured worse than taking none, so the rule is all or nothing.
 */
describe('the reserve the anchored desk asks the chat for', () => {
  const wide = { shown: true, w: 300, chatWidth: 1342 }

  it('asks for the panel, its inset from the edge, and a gap off the text', () => {
    expect(deskReserve(wide)).toBe(300 + DESK_LAUNCHER_EDGE + DESK_TEXT_GAP)
  })

  it('asks for nothing while the desk is down', () => {
    expect(deskReserve({ ...wide, shown: false })).toBe(0)
  })

  it('asks for nothing once the panel has been dragged off its anchor', () => {
    /* The reserve is against the anchored position. A panel over the middle of
       the window is not answered by a margin on the right. */
    expect(deskReserve({ ...wide, detached: true })).toBe(0)
  })

  it('treats an absent detached flag as anchored, the way the markup does', () => {
    /* `DeskGeometry.detached` is optional and `data-anchored={!geom.detached}`
       reads its absence as anchored; a stored geometry from before the flag
       existed arrives that way. */
    expect(deskReserve({ shown: true, w: 300, chatWidth: 1342 })).toBeGreaterThan(0)
  })

  it('asks for nothing rather than part, once the column would go under the floor', () => {
    /* All-or-nothing: at this width taking the whole reserve leaves less than a
       readable column, and taking part of it leaves the column narrower AND
       still under the panel. Standing aside is what the reader is better off
       with, so the boundary is asserted from both sides. */
    const want = 300 + DESK_LAUNCHER_EDGE + DESK_TEXT_GAP
    expect(deskReserve({ shown: true, w: 300, chatWidth: want + DESK_COLUMN_FLOOR })).toBe(want)
    expect(deskReserve({ shown: true, w: 300, chatWidth: want + DESK_COLUMN_FLOOR - 1 })).toBe(0)
  })

  it('gives a wider panel up sooner, because it is asking for more', () => {
    /* The panel is resizable, so the floor is a fact about the pair rather than
       about one width: the same chat that can spare 324px cannot spare 504px. */
    const chat = 324 + DESK_COLUMN_FLOOR
    expect(deskReserve({ shown: true, w: 300, chatWidth: chat })).toBe(324)
    expect(deskReserve({ shown: true, w: 480, chatWidth: chat })).toBe(0)
  })
})
