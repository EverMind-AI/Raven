// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { open } from './approve'
import { _resetForTests, session, sync } from './sheets'

import type { Shell } from '../../shell/bridge'

let openSession: string | null = 'a'

function wire(): void {
  const shell: Shell = {
    T: (key) => key,
    toast: () => {},
    menuAt: () => {},
    confirmAsk: () => {},
    showPage: () => {},
    sessionKey: () => openSession as string,
  }
  window.RavenShell = shell
  document.body.innerHTML =
    '<div class="chat"><div class="dock"><div class="sheets" id="sheetRack"></div>'
    + '<div class="dock-in"></div></div></div>'
}

const rack = (): HTMLElement => document.getElementById('sheetRack')!
const sheets = (): HTMLElement[] => [...rack().querySelectorAll<HTMLElement>('.csheet')]
const opts = (): HTMLElement[] => [...rack().querySelectorAll<HTMLElement>('.opt')]
/* Capture phase, because that is where the handler listens. */
const key = (k: string, over: Partial<KeyboardEventInit> = {}): void => {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, ...over }))
}

beforeEach(() => {
  openSession = 'a'
  _resetForTests()
  wire()
})

afterEach(() => {
  delete window.RavenShell
  document.body.innerHTML = ''
})

describe('the approval sheet', () => {
  it('raises one sheet in the asking conversation, quoting the request', () => {
    open('rm -rf build/')
    expect(sheets().length).toBe(1)
    expect(rack().querySelector('.what')!.textContent).toBe('rm -rf build/')
    expect(sheets()[0]!.dataset.sess).toBe(session())
  })

  it('offers allow first, numbered, and marks it as the default', () => {
    open('do it')
    expect(opts().map((b) => b.textContent)).toEqual(['1gui.confirm.allow', '2gui.confirm.deny'])
    expect(opts()[0]!.className).toContain('go')
    expect(opts()[1]!.className).not.toContain('go')
  })

  it('answers allow on the first option and takes the sheet down', () => {
    const said: string[] = []
    open('do it', () => said.push('allow'), () => said.push('deny'))
    opts()[0]!.click()
    expect(said).toEqual(['allow'])
    expect(sheets().length).toBe(0)
  })

  it('answers deny on the second, and on the close button', () => {
    const said: string[] = []
    open('a', () => said.push('allow'), () => said.push('deny'))
    opts()[1]!.click()
    open('b', () => said.push('allow'), () => said.push('deny'))
    rack().querySelector<HTMLElement>('.ic')!.click()
    expect(said).toEqual(['deny', 'deny'])
  })

  it('reads Escape and the digits as answers', () => {
    const said: string[] = []
    open('a', () => said.push('allow'), () => said.push('deny'))
    key('Escape')
    open('b', () => said.push('allow'), () => said.push('deny'))
    key('1')
    open('c', () => said.push('allow'), () => said.push('deny'))
    key('2')
    expect(said).toEqual(['deny', 'allow', 'deny'])
  })

  /* The turn is blocked on one answer, so a second one must not arrive -- from
     any door. Clicking allow and then pressing Escape used to be reachable in
     the gap before the sheet left the DOM. */
  it('answers once, whichever door is used twice', () => {
    const said: string[] = []
    open('a', () => said.push('allow'), () => said.push('deny'))
    const allow = opts()[0]!
    const deny = opts()[1]!
    allow.click()
    deny.click()
    key('Escape')
    key('2')
    expect(said).toEqual(['allow'])
  })

  /* A sheet parked with another conversation still has its document handler.
     Answering from the keyboard while looking at a different conversation would
     reply on behalf of a turn the reader is not watching. */
  it('ignores the keyboard while its conversation is not the open one', () => {
    const said: string[] = []
    open('a', () => said.push('allow'), () => said.push('deny'))
    openSession = 'b'
    sync()
    key('1')
    key('Escape')
    expect(said).toEqual([])
    /* And it is still answerable when the reader comes back. */
    openSession = 'a'
    sync()
    key('1')
    expect(said).toEqual(['allow'])
  })

  it('leaves an input method alone mid-composition', () => {
    const said: string[] = []
    open('a', () => said.push('allow'), () => said.push('deny'))
    key('1', { isComposing: true })
    key('Escape', { keyCode: 229 })
    expect(said).toEqual([])
    key('1')
    expect(said).toEqual(['allow'])
  })

  /* The withdrawal that makes replacement clean is scoped to one conversation,
     because the rack is. A single page-wide slot took down another
     conversation's pending question with neither callback run -- and that turn
     is still paused on the server, with no UI left that could answer it. The
     reader's only clue would be a conversation that never finishes. */
  it("leaves another conversation's pending question alone", () => {
    const said: string[] = []
    open('A asks', () => said.push('A-allow'), () => said.push('A-deny'))
    openSession = 'b'
    sync()
    open('B asks', () => said.push('B-allow'), () => said.push('B-deny'))
    expect(rack().querySelector('.what')!.textContent).toBe('B asks')

    openSession = 'a'
    sync()
    expect(sheets().length).toBe(1)
    expect(rack().querySelector('.what')!.textContent).toBe('A asks')
    /* Still answerable, and answering it answers A. */
    expect(said).toEqual([])
    key('1')
    expect(said).toEqual(['A-allow'])
  })

  it('replaces the pending question rather than stacking a second', () => {
    open('first')
    open('second')
    expect(sheets().length).toBe(1)
    expect(rack().querySelector('.what')!.textContent).toBe('second')
  })

  /* close() is for a question that stopped mattering -- a cancelled turn -- so
     it must not answer on the reader's behalf. */
  it('closes without answering when the caller withdraws it', () => {
    const said: string[] = []
    const a = open('a', () => said.push('allow'), () => said.push('deny'))
    a.close()
    expect(sheets().length).toBe(0)
    expect(said).toEqual([])
    /* And the withdrawn sheet's handler is gone with it. */
    key('1')
    expect(said).toEqual([])
  })

  /* The one property with no behavioural signature, so it is pinned where it is
     observable: at the registration boundary. A leaked handler cannot produce a
     wrong answer -- the `answered` latch and the `isConnected` check each block
     it on their own -- which is exactly why removing the removeEventListener
     leaves every other case in this file green. What it produces is one document
     listener per approval, for the life of the page, on a screen whose whole job
     is to keep asking.

     Three exits, and they are the three that end in `close`. There is a fourth
     that does not: `sheets.forget(key)`, which a deleted conversation triggers,
     takes the element out of the bucket and the DOM without asking this module,
     so `answered` stays false and the handler stays registered. It is inert --
     the `isConnected` check sees a detached sheet -- but it is retention, and the
     door it needs is a teardown the rack invokes on removal rather than
     bookkeeping kept out here. Named rather than asserted, because the fix is a
     change to the rack. */
  it('unregisters its key handler on every exit that goes through close', () => {
    type Listener = EventListenerOrEventListenerObject
    const live = new Set<Listener>()
    const realAdd = document.addEventListener.bind(document)
    const realRemove = document.removeEventListener.bind(document)
    const spy = (keep: (fn: Listener) => void, pass: typeof realAdd) =>
      ((type: string, fn: Listener, opts?: boolean | object) => {
        if (type === 'keydown') keep(fn)
        pass(type as 'keydown', fn as EventListener, opts as boolean)
      }) as typeof document.addEventListener
    document.addEventListener = spy((fn) => live.add(fn), realAdd)
    document.removeEventListener = spy((fn) => live.delete(fn), realRemove)
    try {
      open('answered', () => {}, () => {})
      expect(live.size).toBe(1)
      opts()[0]!.click()
      expect(live.size).toBe(0)

      const withdrawn = open('withdrawn', () => {}, () => {})
      expect(live.size).toBe(1)
      withdrawn.close()
      expect(live.size).toBe(0)

      /* And the one a new request replaced, which leaves through dropClass
         rather than through either door. */
      open('replaced', () => {}, () => {})
      open('replacing', () => {}, () => {})
      expect(live.size).toBe(1)
    } finally {
      document.addEventListener = realAdd
      document.removeEventListener = realRemove
    }
  })

  it('names itself for a screen reader and takes the focus', () => {
    open('a')
    const sheet = sheets()[0]!
    expect(sheet.getAttribute('role')).toBe('dialog')
    expect(sheet.getAttribute('aria-modal')).toBe('true')
    expect(sheet.getAttribute('aria-label')).toBe('gui.confirm.title')
    expect(document.activeElement).toBe(opts()[0])
  })
})
