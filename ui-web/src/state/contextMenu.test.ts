// @vitest-environment happy-dom
/* One rule for right-click everywhere in the window: the host's menu in a text
 * field or over a selection, the app's menu on a surface that declares actions,
 * and nothing anywhere else.
 */
import { afterEach, describe, expect, it } from 'vitest'

import { nativeCtxOk, onContextMenu } from './contextMenu'

import type { MenuItem } from '../shell/menu'

type Declaring = Element & { _ctx?: () => Array<MenuItem | '-'> }

function markup(): void {
  document.body.innerHTML = [
    '<div class="menu" id="menu" data-open="false"></div>',
    '<div class="dock-in"><textarea id="ta"></textarea></div>',
    '<div class="railtop"><span id="bare">Raven</span></div>',
    '<div id="row"><span id="cell">a session</span></div>',
  ].join('')
}

/** The right-click as the page's one listener sees it. */
function rightClick(el: Element): MouseEvent {
  const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 40, clientY: 50 })
  el.dispatchEvent(event)
  return event
}

afterEach(() => {
  document.removeEventListener('contextmenu', onContextMenu)
  document.body.innerHTML = ''
})

function install(): void {
  markup()
  document.addEventListener('contextmenu', onContextMenu)
}

describe('the right-click rule', () => {
  /* The host menu has paste, dictation, spelling and Look Up, and none of that
     is ours to reimplement -- so in a field the event is left alone. */
  it('leaves a text field to the host menu and opens nothing of its own', () => {
    install()
    const event = rightClick(document.getElementById('ta')!)
    expect(event.defaultPrevented).toBe(false)
    expect(document.getElementById('menu')!.dataset.open).toBe('false')
  })

  it('opens nothing at all on chrome that declares no actions', () => {
    install()
    const event = rightClick(document.getElementById('bare')!)
    /* Prevented, because a menu offering only the host's Services is noise. */
    expect(event.defaultPrevented).toBe(true)
    expect(document.getElementById('menu')!.dataset.open).toBe('false')
  })

  it('opens the row\'s own actions for a click on any part of it', () => {
    install()
    const row = document.getElementById('row') as Declaring
    row._ctx = () => [{ label: 'Rename', fn: () => {} }, '-', { label: 'Delete', bad: true, fn: () => {} }]
    const event = rightClick(document.getElementById('cell')!)
    expect(event.defaultPrevented).toBe(true)
    const host = document.getElementById('menu')!
    expect(host.dataset.open).toBe('true')
    expect(host.style.left).toBe('40px')
    expect(host.style.top).toBe('50px')
  })

  /* The first surface on the way up that declares anything answers, whether or
     not it has rows to offer: a row that decides it has none is still the
     answer, and the surface outside it is not asked. */
  it('stops at the first surface that declares, even when it offers nothing', () => {
    install()
    const row = document.getElementById('row') as Declaring
    const outer = document.body as unknown as Declaring
    row._ctx = () => []
    outer._ctx = () => [{ label: 'Paste', fn: () => {} }]
    try {
      rightClick(document.getElementById('cell')!)
      expect(document.getElementById('menu')!.dataset.open).toBe('false')
    } finally {
      delete outer._ctx
    }
  })

  it('reads a real selection under the pointer as the host\'s business', () => {
    install()
    const cell = document.getElementById('cell')!
    const selection = document.getSelection()!
    const range = document.createRange()
    range.selectNodeContents(cell)
    selection.removeAllRanges()
    selection.addRange(range)
    expect(nativeCtxOk(cell)).toBe(true)
    /* A selection left behind somewhere else is not what the click is about,
       and the rule asks containsNode for exactly that -- but not assertable
       here: happy-dom reads partial containment as "the range starts before
       the node OR ends after it", which every node outside the selection
       satisfies (node_modules/happy-dom/lib/selection/Selection.js). */
    selection.removeAllRanges()
    expect(nativeCtxOk(cell)).toBe(false)
  })
})
