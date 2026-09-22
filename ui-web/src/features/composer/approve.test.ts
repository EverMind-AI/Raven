// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { _resetForTests as sessionReset, setCurrent } from '../../lib/session'
import * as confirmStore from '../../state/confirm'
import * as pageStore from '../../state/page'
import { _resetForTests as draftsReset } from '../../state/sheetDrafts'
import { _resetForTests, add as rackAdd, forget, remove as rackRemove, session, sync } from '../../state/sheetRack'
import { mountPageRoot } from '../../test/pageRoot'
import { _resetForTests as approveReset, closeApproval, open, openApproval } from './approve'

import type { ApprovalHandlers } from './approve'


function wire(): void {
  setTranslator((key) => key)
  vi.spyOn(pageStore, 'show').mockImplementation(() => {})
  vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})
  document.body.innerHTML =
    '<div class="chat"><div class="dock"><div class="sheets" id="sheetRack"></div>'
    + '<div class="dock-in"></div></div></div>'
  /* The sheets render from the page's own root (src/chrome/SheetRack.tsx), so it
     has to be standing before one is raised. */
  unmount = mountPageRoot()
}

let unmount: (() => void) | null = null

const rack = (): HTMLElement => document.getElementById('sheetRack')!
const sheets = (): HTMLElement[] => [...rack().querySelectorAll<HTMLElement>('.csheet')]
const opts = (): HTMLElement[] => [...rack().querySelectorAll<HTMLElement>('.opt')]
/* Capture phase, because that is where the handler listens. */
const key = (k: string, over: Partial<KeyboardEventInit> = {}): void => {
  document.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, ...over }))
}

beforeEach(() => {
  sessionReset()
  setCurrent('a')
  _resetForTests()
  draftsReset()
  wire()
})

afterEach(() => {
  if (unmount) unmount()
  unmount = null
  approveReset()
  sessionReset()
  resetTranslator()
  document.body.innerHTML = ''
})

describe('the approval sheet', () => {
  it('raises one sheet in the asking conversation, quoting the request', () => {
    open('rm -rf build/')
    expect(sheets().length).toBe(1)
    expect(rack().querySelector('.what')!.textContent).toBe('rm -rf build/')
    expect(sheets()[0]!.dataset.sess).toBe(session())
  })

  it('marks itself as asking, so whatever else is docked can step aside', () => {
    /* The reader cannot get on until they answer this, and the rack is shared --
       a running graph is tall enough to push the question below the fold. The
       sweeps read the mark to know what they may replace. */
    open('rm -rf build/')

    expect(sheets()[0]!.dataset.asks).toBe('1')
  })

  /* Which conversation asked is the caller's to say, because it is not always
     the one on screen: a turn the reader stepped away from can block on an
     approval at any moment. Filed under the open conversation instead, the
     question docks over a conversation it has nothing to do with -- and the one
     that asked shows nothing pending while its turn stays paused on the server
     waiting for the answer. */
  it('files the request under the conversation named by the caller', () => {
    setCurrent('b')
    open('rm -rf build/', () => {}, () => {}, 'a')

    /* Nothing over the composer the reader is actually looking at. */
    expect(sheets().length).toBe(0)

    setCurrent('a')
    sync()
    expect(sheets().length).toBe(1)
    expect(sheets()[0]!.dataset.sess).toBe('a')
    expect(rack().querySelector('.what')!.textContent).toBe('rm -rf build/')
  })

  it('docks where the reader is when the caller names no conversation', () => {
    setCurrent('b')
    open('rm -rf build/')
    expect(sheets()[0]!.dataset.sess).toBe('b')
  })

  /* The named conversation has to reach every scoped call, not just the filing:
     a request for A while B is on screen must not withdraw B's pending question,
     and must not be answerable off the keyboard of the reader looking at B. */
  it('scopes replacement and the keyboard to the conversation that asked', () => {
    const said: string[] = []
    setCurrent('b')
    open('B asks', () => said.push('B-allow'), () => said.push('B-deny'))
    open('A asks', () => said.push('A-allow'), () => said.push('A-deny'), 'a')

    /* B's question is untouched, and it is still B's that answers here. */
    expect(sheets().length).toBe(1)
    expect(rack().querySelector('.what')!.textContent).toBe('B asks')
    key('1')
    expect(said).toEqual(['B-allow'])

    setCurrent('a')
    sync()
    expect(rack().querySelector('.what')!.textContent).toBe('A asks')
    key('1')
    expect(said).toEqual(['B-allow', 'A-allow'])
  })

  /* The same scoping, in the order that catches the withdrawal handle rather
     than the filing: the away request is registered FIRST, so a subsequent
     request in the open conversation is what would reach for it. Registered
     under the open conversation instead, this is a silent withdrawal -- no
     callback runs, the sheet is gone, and A's turn waits on the server for an
     answer no UI can give any more. */
  it('registers the withdrawal handle under the conversation that asked', () => {
    const said: string[] = []
    setCurrent('b')
    open('A asks', () => said.push('A-allow'), () => said.push('A-deny'), 'a')
    open('B asks', () => said.push('B-allow'), () => said.push('B-deny'))

    setCurrent('a')
    sync()
    expect(sheets().length).toBe(1)
    expect(rack().querySelector('.what')!.textContent).toBe('A asks')
    expect(said).toEqual([])
    key('1')
    expect(said).toEqual(['A-allow'])
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
    setCurrent('b')
    sync()
    key('1')
    key('Escape')
    expect(said).toEqual([])
    /* And it is still answerable when the reader comes back. */
    setCurrent('a')
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
    setCurrent('b')
    sync()
    open('B asks', () => said.push('B-allow'), () => said.push('B-deny'))
    expect(rack().querySelector('.what')!.textContent).toBe('B asks')

    setCurrent('a')
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
  it('unregisters its key handler on every exit, the one the rack owns included', () => {
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

      /* The exit this module is not told about: the conversation is deleted
         while its question is still on screen. `forget` removes the sheet
         straight from the rack, so nothing here runs unless the rack runs it --
         which is the whole reason the takedown is registered there. */
      forget(session())
      expect(live.size).toBe(0)
    } finally {
      document.addEventListener = realAdd
      document.removeEventListener = realRemove
    }
  })

  /* Deleting the conversation is not answering the question: neither callback
     may run, and nothing may be left listening on behalf of a conversation that
     no longer exists. */
  it('takes a pending question down with its conversation, answering neither way', () => {
    const said: string[] = []
    open('mid-question', () => said.push('allow'), () => said.push('deny'))
    expect(sheets().length).toBe(1)
    forget('a')
    expect(sheets().length).toBe(0)
    expect(said).toEqual([])
    /* And the keyboard is no longer answering for it. */
    key('1')
    expect(said).toEqual([])
  })

  /* The teardown ends in this module's own close, which calls back into the
     rack's remove. Whatever order those two do their work in, one pass has to
     be the end of it. */
  it('takes a sheet down once, though its takedown re-enters the rack', () => {
    let ran = 0
    const a = open('once', () => { ran += 1 }, () => { ran += 1 })
    a.close()
    a.close()
    expect(ran).toBe(0)
    expect(sheets().length).toBe(0)
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

describe('the permission approval sheet', () => {
  const base = {
    approvalId: 'ap-1', command: 'rm file.txt', description: 'Delete files',
    kind: 'shell.exec', family: 'delete_command', origin: { kind: 'user', name: '' },
    evidence: { command: 'rm file.txt', cwd: '/w' },
  }
  const suggested = {
    ...base, approvalId: 'ap-s', command: 'git push origin HEAD', family: 'publish_command', suggestedPattern: 'git push *',
    evidence: { command: 'git push origin HEAD', cwd: '/w' },
  }
  /* One sheet per request id, so a case that opens several gives each its own. */
  let n = 0
  const fresh = <T extends { approvalId: string }>(req: T): T => ({ ...req, approvalId: `${req.approvalId}-${++n}` })
  const sheets = () => [...document.querySelectorAll<HTMLElement>('.csheet')]
  const opts = () => [...document.querySelectorAll<HTMLButtonElement>('.csheet .opt')]
  const landed = () => document.querySelector<HTMLElement>('.cp-land')
  const said: Array<[string, string, string | undefined]> = []
  const handlers = (more: Partial<ApprovalHandlers> = {}): ApprovalHandlers => ({
    onChoice: (c, f, p) => { said.push([c, f, p]) },
    ...more,
  })
  const tick = () => new Promise((r) => setTimeout(r, 0))

  beforeEach(() => { said.length = 0 })

  it('words the question by the family and quotes the command as the evidence', () => {
    openApproval(base, handlers())
    expect(sheets()[0]!.getAttribute('aria-label')).toBe('gui.confirm.title.delete_command')
    expect(document.querySelector('.csheet .q')!.textContent).toBe('gui.confirm.title.delete_command')
    expect(document.querySelector('.cp-why')!.textContent).toBe('gui.confirm.why.delete_command')
    expect(document.querySelector('.csheet .what')!.textContent).toBe('rm file.txt')
    expect(sheets()[0]!.dataset.asks).toBe('1')
  })

  it('offers deny first and focused, allow once last, and one broader grant between them', () => {
    /* No rule can be written for this one, so the middle answer is the grant
       that lasts as long as the conversation -- the only thing that stops the
       same question repeating through a task. */
    openApproval(base, handlers())
    expect(opts().map((b) => b.textContent)).toEqual(
      ['1gui.confirm.deny', '2gui.confirm.allow_session', '3gui.confirm.allow'])
    expect(opts()[0]!.className).toContain('go')
    expect(document.activeElement).toBe(opts()[0])

    /* A rule can be written for this one, and it takes that middle slot rather
       than adding a fourth answer beside it. */
    openApproval(suggested, handlers())
    expect(opts().map((b) => b.textContent)).toEqual(
      ['1gui.confirm.deny', '2gui.confirm.always', '3gui.confirm.allow'])
  })

  it('sends the session grant the engine knows by name', () => {
    openApproval(base, handlers())
    opts()[1]!.click()
    expect(said).toEqual([['allow_session', '', undefined]])
  })

  /* The one sweep that could still strand a turn. A confirm request arriving on
     the same conversation used to take the gate's pending ask down with it, and
     nothing under that ask retires it but an answer: the call would then wait
     for the day-long floor. Confirm and clarify both have deadlines of their
     own, so sparing this one costs nothing and losing it costs the turn. */
  it('is left standing when a confirm request arrives on the same conversation', () => {
    const said: string[] = []
    openApproval(base, handlers({ onChoice: (c) => { said.push(c) } }))
    expect(sheets().length).toBe(1)

    open('rm -rf build/', () => said.push('confirm-yes'), () => said.push('confirm-no'))
    expect(sheets().length).toBe(2)
    /* And it is still the one that can be answered. */
    const gate = sheets().find((el) => el.dataset.noDeadline === '1')!
    expect(gate).toBeTruthy()
    expect(gate.querySelector('.opt')).toBeTruthy()
  })

  /* The landed sheet after a refusal carries a field. A reader typing into it
     for longer than the linger would have watched it vanish mid-sentence, with
     nothing sent and the words gone. */
  /* The gate cuts an oversized account down to what a person reads
     (PermissionGate._clamped) and marks it. Rendering the clipped value without
     the mark is the one thing the cap must not cause: the reader would answer
     about a change they can only see part of, with nothing saying so. */
  it('says so in every layout when the engine shortened the evidence', () => {
    const cases = [
      { kind: 'file.write', evidence: { path: '/w/big.py', created: false, diff: '-a\n+b', truncated: true } },
      { kind: 'mcp.call', evidence: { server: 's', tool: 't', input: 'x'.repeat(40), truncated: true } },
      { kind: 'shell.exec', evidence: { command: 'rm -rf x', cwd: '/w', truncated: true } },
      { kind: 'unknown', evidence: { input: 'y'.repeat(40), truncated: true } },
    ]
    for (const c of cases) {
      openApproval(fresh({ ...base, ...c, family: '' }), handlers())
      expect(document.querySelector('.cp-ev-cut')?.textContent, c.kind).toBe('gui.confirm.ev.cut')
    }
    /* And stays quiet when nothing was cut. */
    openApproval(fresh({ ...base, kind: 'file.write', family: '', evidence: { path: '/w/a.py', diff: '-a\n+b' } }), handlers())
    expect(document.querySelector('.cp-ev-cut')).toBeNull()
  })

  it('lays out a write as its path and diff, an MCP call as its tool and input, the rest as arguments', () => {
    const diff = '--- a\n+++ b\n@@ -1 +1 @@\n-x = 1\n+x = 2'
    openApproval(fresh({ ...base, kind: 'file.write', family: '', evidence: { path: '/w/a.py', created: false, diff } }), handlers())
    expect(document.querySelector('.csheet .q')!.textContent).toBe('gui.confirm.title.file_write')
    expect(document.querySelector('.cp-ev-path')!.textContent).toBe('/w/a.py')
    expect([...document.querySelectorAll('.cp-diff .cp-add')].map((e) => e.textContent!.trim())).toEqual(['+x = 2'])
    expect([...document.querySelectorAll('.cp-diff .cp-del')].map((e) => e.textContent!.trim())).toEqual(['-x = 1'])
    /* The file header repeats the path line; the room goes to the change. */
    expect(document.querySelector('.cp-diff')!.textContent).not.toContain('--- a')

    openApproval(fresh({ ...base, kind: 'file.write', family: '', evidence: { path: '/w/new.py', created: true } }), handlers())
    expect(document.querySelector('.cp-ev-path')!.textContent).toBe('/w/new.py · gui.confirm.ev.created')
    expect(document.querySelector('.cp-ev-none')!.textContent).toBe('gui.confirm.ev.nodiff')

    openApproval(fresh({
      ...base, kind: 'mcp.call', family: '',
      evidence: { server: 'notion', tool: 'create_page', input: { title: 'weekly' } },
    }), handlers())
    expect(document.querySelector('.csheet .q')!.textContent).toBe('gui.confirm.title.mcp_call')
    expect(document.querySelector('.cp-ev-path')!.textContent).toBe('notion.create_page')
    expect(document.querySelector('.cp-json')!.textContent).toContain('"title": "weekly"')

    openApproval(fresh({ ...base, kind: 'unknown', family: '', evidence: { input: { n: 3 } } }), handlers())
    expect(document.querySelector('.csheet .q')!.textContent).toBe('gui.confirm.title.unknown')
    expect(document.querySelector('.cp-json')!.textContent).toContain('"n": 3')
  })

  it('names a sub-agent as the one asking, and Raven otherwise', () => {
    setTranslator((key, vars) => `${key}|${String(vars?.who ?? '')}`)
    openApproval(fresh({ ...base, origin: { kind: 'subagent', name: 'raven-code' } }), handlers())
    expect(document.querySelector('.cp-why')!.textContent).toBe('gui.confirm.why.delete_command|raven-code')
    openApproval(fresh(base), handlers())
    expect(document.querySelector('.cp-why')!.textContent).toBe('gui.confirm.why.delete_command|Raven')
  })

  it('answers at once and leaves nothing behind when there is nothing to do', () => {
    openApproval(base, handlers())
    opts()[2]!.click()
    expect(said).toEqual([['allow', '', undefined]])
    /* The sheet going is the receipt. A line that only repeats the press is one
       more thing to read, and the rack is where the reader is trying to work. */
    expect(sheets().length).toBe(0)
    expect(landed()).toBeNull()
  })

  it('leaves nothing behind after a refusal either', () => {
    openApproval(base, handlers())
    opts()[0]!.click()
    expect(said).toEqual([['deny', '', undefined]])
    expect(sheets().length).toBe(0)
  })

  it('sends the suggested rule with a saved grant, and the landed sheet can take it back', async () => {
    /* The undo says only "take back what this answer wrote" -- it carries no
       pattern, because what the answer put on disk is the engine's to know. */
    let undone = 0
    openApproval(suggested, handlers({ onRevoke: async () => { undone += 1; return true } }))
    opts()[1]!.click()
    expect(said).toEqual([['allow_always', '', 'git push *']])
    expect(landed()!.querySelector('.cp-land-text')!.textContent).toBe('gui.confirm.land.always')

    landed()!.querySelector<HTMLButtonElement>('.cp-undo')!.click()
    await tick()
    expect(undone).toBe(1)
    expect(landed()!.textContent).toBe('gui.confirm.land.revoked')
    expect(landed()!.querySelector('.cp-undo')).toBeNull()
  })

  it('says so when the rule could not be taken back', async () => {
    openApproval(fresh(suggested), handlers({ onRevoke: async () => false }))
    opts()[1]!.click()
    landed()!.querySelector<HTMLButtonElement>('.cp-undo')!.click()
    await tick()
    expect(landed()!.textContent).toBe('gui.confirm.land.revoke_failed')
  })

  it('reads Escape and the digits, deny being the default', () => {
    openApproval(fresh(suggested), handlers())
    key('Escape')
    openApproval(fresh(suggested), handlers())
    key('3')
    openApproval(fresh(suggested), handlers())
    key('2')
    expect(said.map(([c]) => c)).toEqual(['deny', 'allow', 'allow_always'])
  })

  it('answers once, whichever door is used twice', () => {
    openApproval(base, handlers())
    const [deny, , allow] = opts()
    allow!.click()
    deny!.click()
    key('Escape')
    expect(said.map(([c]) => c)).toEqual(['allow'])
  })

  it('ignores the keyboard while its conversation is not the open one', () => {
    openApproval(base, handlers())
    setCurrent('b')
    sync()
    key('1')
    key('Escape')
    expect(said).toEqual([])
    setCurrent('a')
    sync()
    key('3')
    expect(said.map(([c]) => c)).toEqual(['allow'])
  })

  it('withdraws silently when the server closes the request, and leaves a landed answer alone', () => {
    openApproval(base, handlers())
    closeApproval('ap-1')
    expect(sheets().length).toBe(0)
    expect(said).toEqual([])

    openApproval(suggested, handlers())
    opts()[1]!.click()
    closeApproval('ap-s')
    expect(landed()).not.toBeNull()
  })

  it('takes a landed sheet down with the next request in the same conversation', () => {
    openApproval(suggested, handlers())
    opts()[1]!.click()
    expect(landed()).not.toBeNull()
    openApproval({ ...base, approvalId: 'ap-2' }, handlers())
    expect(landed()).toBeNull()
    expect(sheets().length).toBe(1)
  })

  it('says so when the engine did not take the answer, where an answer that landed says nothing', async () => {
    openApproval(base, handlers({ onChoice: () => Promise.resolve(false) }))
    opts()[2]!.click()
    /* Nothing yet: as far as the page knows the answer was taken. */
    expect(landed()).toBeNull()
    await tick()
    expect(landed()!.textContent).toBe('gui.confirm.land.unsent')

    openApproval({ ...base, approvalId: 'ap-2' }, handlers({ onChoice: () => Promise.reject(new Error('socket')) }))
    opts()[2]!.click()
    await tick()
    expect(landed()!.textContent).toBe('gui.confirm.land.unsent')
  })

  it('reads no digit typed into a field, where it is text -- Escape still refuses', () => {
    const ta = document.createElement('textarea')
    document.body.appendChild(ta)
    openApproval(suggested, handlers())
    ta.dispatchEvent(new KeyboardEvent('keydown', { key: '2', bubbles: true }))
    ta.dispatchEvent(new KeyboardEvent('keydown', { key: '3', bubbles: true }))
    expect(said).toEqual([])
    ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(said.map(([c]) => c)).toEqual(['deny'])
  })

  it('leaves the keyboard to a sheet docked above it, and takes it back when that one goes', () => {
    openApproval(base, handlers())
    const above = document.createElement('div')
    above.className = 'csheet'
    rackAdd(above, session())
    key('2')
    expect(said).toEqual([])
    rackRemove(above)
    key('3')
    expect(said.map(([c]) => c)).toEqual(['allow'])
  })

  it('opens one sheet per request, so a replay after a reload does not stack a second', () => {
    openApproval(base, handlers())
    openApproval(base, handlers())
    expect(sheets().length).toBe(1)
    opts()[2]!.click()
    expect(said.map(([c]) => c)).toEqual(['allow'])
  })
})
