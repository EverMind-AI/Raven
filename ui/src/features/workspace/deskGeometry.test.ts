/** Tests for floating workspace geometry and column transitions. */

// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import {
  anchoredGeometry,
  DESK_ANCHOR_GAP,
  DESK_DEFAULT_HEIGHT,
  DESK_DEFAULT_WIDTH,
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
