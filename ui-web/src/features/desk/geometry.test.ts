/** Tests for floating workspace geometry and column transitions. */

// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import {
  anchoredGeometry,
  anchoredRoom,
  clampGeometry,
  DESK_ANCHOR_GAP,
  DESK_COLUMN_FLOOR,
  DESK_COMPOSER_GAP,
  DESK_LAUNCHER_EDGE,
  DESK_TEXT_GAP,
  deskReserve,
  DESK_DEFAULT_HEIGHT,
  DESK_DEFAULT_WIDTH,
  DESK_MAX_WIDTH,
  DESK_MIN_SIZE,
  DESK_VIEWPORT_GUTTER,
  defaultGeometry,
  workspaceColumnCount,
  workspaceTransitionWidth,
} from './geometry'

/* The launcher, at the size and place the stylesheet gives it: a 30px button
   centred on y=27, 50px in from the right edge: the 12px gutter plus the
   header slot the conversation menu takes. */
const EDGE = 50

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

/* The composer card, where the stylesheet puts it: the last child of `.dock`,
   which hangs off the chat's floor. happy-dom lays nothing out, so its
   rectangle is stated. By default it ends at x=600, clear of a 300px palette
   hanging from the launcher at 974 -- where the reserve puts it in a chat wide
   enough to spare one. */
function composer(bottom: number, { height = 132, left = 36, right = 600 } = {}): HTMLElement {
  document.body.insertAdjacentHTML('beforeend', '<div class="chat"><div class="dock"><div class="dock-in"></div></div></div>')
  const card = document.querySelector('.chat > .dock > .dock-in') as HTMLElement
  card.getBoundingClientRect = () => ({
    left, right, top: bottom - height, bottom,
    width: right - left, height, x: left, y: bottom - height, toJSON: () => ({}),
  }) as DOMRect
  return card
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
    expect(DESK_DEFAULT_WIDTH).toBeLessThanOrEqual(DESK_MAX_WIDTH)
  })

  it('snaps a dragged desk back to that same place', () => {
    const right = launcher()
    const back = anchoredGeometry({ x: 300, y: 400, w: 260, h: 300, detached: true })
    expect(back.x + back.w).toBe(right)
    expect(back.y).toBe(42 + DESK_ANCHOR_GAP)
    expect(back.detached).toBe(false)
  })
})

/* How far down the desk reaches: to the composer card, not to a number. A
 * fixed height ends the palette wherever that number happens to land
 * -- in one reader's window, level with a message bubble and well short of
 * the box they type in.
 */
describe('how far down the desk reaches', () => {
  it('lets an anchored desk reach the composer card\'s bottom edge, and no further', () => {
    launcher()
    composer(700)
    const at = clampGeometry({ x: 0, y: 42 + DESK_ANCHOR_GAP, w: 300, h: 5000, detached: false })
    expect(at.h).toBe(700 - (42 + DESK_ANCHOR_GAP))
    /* Only the height: the width stops where it always has. */
    expect(clampGeometry({ ...at, w: 5000 }).w).toBe(DESK_MAX_WIDTH)
  })

  it('measures an anchored desk from under the launcher, whatever y it carries', () => {
    /* The stylesheet places an anchored palette. Its stored y is only what
       was last written there, and need not be where the palette is drawn. */
    launcher()
    composer(700)
    const at = clampGeometry({ x: 0, y: DESK_VIEWPORT_GUTTER, w: 300, h: 5000, detached: false })
    expect(at.h).toBe(700 - (42 + DESK_ANCHOR_GAP))
  })

  it('stops a dragged desk at the composer card\'s bottom edge without shrinking it', () => {
    /* A height bounded by the room under the palette's top would make a
       downward move shrink the palette instead of stopping it. */
    composer(700)
    expect(clampGeometry({ x: 40, y: 600, w: 300, h: 300, detached: true })).toMatchObject({ y: 400, h: 300 })
  })

  it('reaches the window\'s own gutter when the composer is not drawn', () => {
    /* An imported conversation has no dock (`.chat[data-readonly] .dock`), and
       a card that is not drawn measures as a zero box -- taken as the limit, it
       would hold the palette at its minimum height. */
    launcher()
    const card = composer(0, { height: 0 })
    card.getClientRects = () => ({ length: 0, item: () => null, [Symbol.iterator]: [][Symbol.iterator] }) as unknown as DOMRectList
    const at = clampGeometry({ x: 0, y: 42 + DESK_ANCHOR_GAP, w: 300, h: 5000, detached: false })
    expect(at.h).toBe(window.innerHeight - DESK_VIEWPORT_GUTTER - (42 + DESK_ANCHOR_GAP))
  })

  it('stops an anchored desk short of a composer card that runs under it', () => {
    /* The landing page and a chat too narrow for the reserve leave the card
       running under the palette's column, where reaching the card's bottom edge
       would put the palette on the field and the send button. */
    launcher()
    composer(700, { right: 836 })
    expect(anchoredRoom(300)).toBe(700 - 132 - DESK_COMPOSER_GAP - (42 + DESK_ANCHOR_GAP))
  })

  it('asks about the column the palette takes at the width it has', () => {
    /* From the launcher's right edge at 974: a card ending at x=700 runs under a
       300px palette, meets a 274px one edge to edge, which is beside it, and
       stays clear of a 250px one. */
    launcher()
    composer(700, { right: 700 })
    expect(anchoredRoom(300)).toBe(700 - 132 - DESK_COMPOSER_GAP - (42 + DESK_ANCHOR_GAP))
    expect(anchoredRoom(274)).toBe(700 - (42 + DESK_ANCHOR_GAP))
    expect(anchoredRoom(250)).toBe(700 - (42 + DESK_ANCHOR_GAP))
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
      splitWidth: 1630,
    })).toBe(1054)
  })

  it('clamps a second column to the available viewport width', () => {
    expect(workspaceTransitionWidth({
      previousWidth: 720,
      previousColumns: 1,
      nextColumns: 2,
      availableWidth: 970,
      splitWidth: 1400,
    })).toBe(970)
  })

  /* A task pane's board is sized like the agent conversation's -- 440, not
     the file pane's 960 -- because that is the width its own card grid was
     laid out against. */
  it('opens a task pane at the agent width, not the file width', () => {
    expect(workspaceTransitionWidth({
      previousWidth: 0,
      previousColumns: 0,
      nextColumns: 1,
      availableWidth: 1200,
      splitWidth: 1630,
      firstPane: { id: 'p1', kind: 'task', row: {} as never },
    })).toBe(440)
  })

  /* A file used to open at a fixed 960, which on a laptop took nearly all of
     the split and left the composer too narrow to write in. */
  it('opens a file pane at half of the split, so the chat keeps the other half', () => {
    const file = { id: 'p1', kind: 'file', path: 'a.md' } as never
    expect(workspaceTransitionWidth({
      previousWidth: 0, previousColumns: 0, nextColumns: 1, availableWidth: 570, splitWidth: 1000, firstPane: file,
    })).toBe(500)
    expect(workspaceTransitionWidth({
      previousWidth: 0, previousColumns: 0, nextColumns: 1, availableWidth: 1400, splitWidth: 1830, firstPane: file,
    })).toBe(915)
  })

  it('reopens at the width the reader last dragged it to, within what is available', () => {
    const file = { id: 'p1', kind: 'file', path: 'a.md' } as never
    const task = { id: 'p1', kind: 'task', row: {} as never } as never
    const open = (firstPane: never, rememberedWidth: number | null, availableWidth = 1400): number =>
      workspaceTransitionWidth({ previousWidth: 0, previousColumns: 0, nextColumns: 1, availableWidth, splitWidth: 1830, rememberedWidth, firstPane })
    expect(open(file, 640)).toBe(640)
    expect(open(task, 640)).toBe(640)
    expect(open(file, 1300, 900)).toBe(900)
    expect(open(file, null)).toBe(915)
  })

  it('never opens a file pane narrower than the pane floor', () => {
    expect(workspaceTransitionWidth({
      previousWidth: 0, previousColumns: 0, nextColumns: 1, availableWidth: 600, splitWidth: 600,
      firstPane: { id: 'p1', kind: 'file', path: 'a.md' } as never,
    })).toBe(320)
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
       about one width: the same chat that can spare 362px cannot spare 542px. */
    const chat = 362 + DESK_COLUMN_FLOOR
    expect(deskReserve({ shown: true, w: 300, chatWidth: chat })).toBe(362)
    expect(deskReserve({ shown: true, w: 480, chatWidth: chat })).toBe(0)
  })
})
