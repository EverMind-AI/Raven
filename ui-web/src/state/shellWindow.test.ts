// @vitest-environment happy-dom
/* Moving and zooming the app's own window from the header band.
 *
 * Nothing here runs in a browser: the desktop shell marks itself on the root
 * element, and the native bridge is only there to take the message when it
 * does -- so every case below says which of those two is missing.
 */
import { afterEach, describe, expect, it } from 'vitest'

import { band, onDblClick, onMouseDown } from './shellWindow'

type Native = Window & { webkit?: { messageHandlers?: { raven?: { postMessage(value: unknown): void } } } }

let posted: unknown[] = []

function page(): void {
  document.body.innerHTML = [
    '<div class="top"><span id="title">A session</span>',
    '<button id="wsBtn">Workspace</button>',
    '<div class="grip" id="wsGrip"></div></div>',
    '<div class="railtop"><span id="wordmark">Raven</span></div>',
  ].join('')
}

/** The shell as it presents itself: the mark on the root, and the bridge. */
function asShell(): void {
  document.documentElement.dataset.shell = '1'
  ;(window as Native).webkit = { messageHandlers: { raven: { postMessage: (m) => posted.push(m) } } }
}

/* Both are the page's own document listeners, registered once and never taken
   off (state/globalListeners.ts), so they go on once for the file. */
document.addEventListener('mousedown', onMouseDown)
document.addEventListener('dblclick', onDblClick)

const grab = (el: Element, button = 0): void => {
  el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button }))
}

afterEach(() => {
  posted = []
  delete document.documentElement.dataset.shell
  delete (window as Native).webkit
  document.body.innerHTML = ''
})

describe('the shell window band', () => {
  it('asks the shell to drag the window when the band itself is grabbed', () => {
    page()
    asShell()
    grab(document.getElementById('title')!)
    expect(posted).toEqual([{ type: 'drag' }])
  })

  it('asks it to zoom on a double-click in the band', () => {
    page()
    asShell()
    document.querySelector('.railtop')!.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, button: 0 }))
    expect(posted).toEqual([{ type: 'zoom' }])
  })

  /* A control in the band is a control: a grab on it is the button's, not the
     window's, or the header would be undraggable AND unclickable. */
  it('leaves a control and a grip in the band alone', () => {
    page()
    asShell()
    grab(document.getElementById('wsBtn')!)
    grab(document.getElementById('wsGrip')!)
    expect(posted).toEqual([])
  })

  it('says nothing in a browser, where the root carries no mark', () => {
    page()
    ;(window as Native).webkit = { messageHandlers: { raven: { postMessage: (m) => posted.push(m) } } }
    expect(band(new MouseEvent('mousedown', { button: 0 }))).toBe(false)
    grab(document.getElementById('title')!)
    expect(posted).toEqual([])
  })

  it('says nothing in a shell with no bridge to say it to', () => {
    page()
    document.documentElement.dataset.shell = '1'
    expect(() => grab(document.getElementById('title')!)).not.toThrow()
  })

  /* Only the primary button: a right-click in the band is the context menu's
     (state/contextMenu.ts). */
  it('ignores every button but the first', () => {
    page()
    asShell()
    grab(document.getElementById('title')!, 2)
    expect(posted).toEqual([])
  })

  it('ignores a grab outside the three bands', () => {
    page()
    asShell()
    const loose = document.createElement('p')
    document.body.appendChild(loose)
    grab(loose)
    expect(posted).toEqual([])
  })
})
