// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { _resetForTests, add, dropClass, forget, remove, session, sync } from './sheets'

import type { Shell } from '../../shell/bridge'

/* The page's open conversation, which the rack asks for rather than keeps. */
let open: string | null = null

function wire(): void {
  const shell: Shell = {
    T: (key) => key,
    toast: () => {},
    menuAt: () => {},
    confirmAsk: () => {},
    showPage: () => {},
    sessionKey: () => open as string,
  }
  window.RavenShell = shell
  /* `.chat` and `.dock` because dockLift measures them; without both it returns
     early, which would make every assertion below pass for the wrong reason. */
  document.body.innerHTML =
    '<div class="chat"><div class="dock"><div class="sheets" id="sheetRack"></div>'
    + '<div class="dock-in"></div></div></div>'
}

const sheet = (cls = 'csheet'): HTMLElement => {
  const n = document.createElement('div')
  n.className = cls
  return n
}
const rack = (): HTMLElement => document.getElementById('sheetRack')!
const mounted = (): string[] => [...rack().children].map((n) => (n as HTMLElement).dataset.sess as string)

beforeEach(() => {
  open = 'a'
  _resetForTests()
  wire()
})

afterEach(() => {
  delete window.RavenShell
  document.body.innerHTML = ''
})

describe('the sheet rack', () => {
  it('files a sheet under the open conversation and mounts it', () => {
    const s = sheet()
    add(s)
    expect(s.dataset.sess).toBe('a')
    expect(mounted()).toEqual(['a'])
  })

  it('keys a sheet raised during a draft, rather than sharing one key', () => {
    open = null
    add(sheet())
    expect(mounted()).toEqual(['(draft)'])
    /* And the draft's key is its own: a second draft-less page state must not
       inherit the first one's pending question. */
    expect(session()).toBe('(draft)')
  })

  it('stacks the newest on top, above the field it interrupts', () => {
    const first = sheet()
    const second = sheet()
    first.id = 'first'
    second.id = 'second'
    add(first)
    add(second)
    expect([...rack().children].map((n) => n.id)).toEqual(['second', 'first'])
  })

  it('files a sheet for another conversation without mounting it', () => {
    add(sheet(), 'b')
    expect(mounted()).toEqual([])
  })

  it('detaches on a switch and mounts the same element back, not a fresh one', () => {
    const s = sheet()
    add(s)
    const field = document.createElement('input')
    /* A half-typed answer is the reason the element is kept rather than rebuilt. */
    s.appendChild(field)
    field.value = 'half typed'

    open = 'b'
    sync()
    expect(mounted()).toEqual([])
    expect(s.isConnected).toBe(false)

    open = 'a'
    sync()
    expect(rack().firstChild).toBe(s)
    expect((s.firstChild as HTMLInputElement).value).toBe('half typed')
  })

  it('mounts a sheet raised while the reader was away', () => {
    open = 'b'
    add(sheet(), 'a')
    open = 'a'
    sync()
    expect(mounted()).toEqual(['a'])
  })

  it('drops one class in one conversation and leaves the other alone', () => {
    const here = sheet('csheet')
    const alsoHere = sheet('dagsheet')
    add(here)
    add(alsoHere)
    add(sheet('csheet'), 'b')

    dropClass('csheet')
    expect([...rack().children].map((n) => (n as HTMLElement).className)).toEqual(['dagsheet'])
    /* The other conversation still has its pending question. */
    open = 'b'
    sync()
    expect(mounted()).toEqual(['b'])
  })

  it('forgets a deleted conversation, mounted or not', () => {
    add(sheet())
    add(sheet(), 'b')
    forget('a')
    expect(mounted()).toEqual([])
    open = 'b'
    sync()
    expect(mounted()).toEqual(['b'])
    forget('b')
    expect(mounted()).toEqual([])
    /* Unmounting is not forgetting. Leaving the bucket behind would let the
       next sync put a deleted conversation's question back on screen, and the
       assertion above cannot tell the two apart on its own. */
    open = 'a'
    sync()
    expect(mounted()).toEqual([])
    open = 'b'
    sync()
    expect(mounted()).toEqual([])
  })

  /* A sync of what is already mounted must not touch the DOM, and the property
     that catches it is focus rather than order. Re-inserting every element would
     leave the order alone -- prepending in bucket order reverses the bucket, and
     the rack is already that reverse, so the two cancel -- but moving a node
     blurs whatever is focused inside it. A sync runs on every session change,
     and the reader may be mid-word in the answer. */
  it('keeps the caret in a mounted sheet when the rack syncs', () => {
    /* Two, because one is not enough to catch it: with a single sheet the
       re-insertion is `insertBefore(el, el)`, which the DOM defines as a no-op,
       so nothing moves and the assertion holds either way. With two, the first
       one actually travels. */
    const first = sheet()
    const second = sheet()
    add(first)
    add(second)
    const field = document.createElement('input')
    first.appendChild(field)
    field.focus()
    expect(document.activeElement).toBe(field)
    sync()
    expect(document.activeElement).toBe(field)
  })

  it('removes a sheet from its bucket, so a later sync cannot resurrect it', () => {
    const s = sheet()
    add(s)
    remove(s)
    expect(mounted()).toEqual([])
    sync()
    expect(mounted()).toEqual([])
  })

  /* The one claim the cases above cannot see. "Every mutation here ends in
     dockLift()" is a third of the argument for this living in the composer
     island, and it is measurement rather than markup -- deleting all three
     calls leaves every other test in this file green.

     What escapes is the back-to-bottom pill and the chat's bottom clearance
     going stale: --lift keeps whatever the last dock resize left it at, so the
     pill parks over a sheet that was just raised, or reserves a gap for one
     that was just retired.

     Presence, not value: happy-dom reports every rect as zero, so the measured
     number is always `0px` and asserting on it would prove nothing. An unset
     property and a written one are still different, which is enough -- hence
     the clear before each leg. dockLift only writes on a change, so without it
     the second leg would pass on the first leg's value. */
  it('lifts the dock after every change to the rack', () => {
    const chat = document.querySelector('.chat') as HTMLElement
    const lifted = (): boolean => chat.style.getPropertyValue('--lift') !== ''
    const clear = (): void => { chat.style.removeProperty('--lift') }
    const s = sheet()

    clear()
    add(s)
    expect(lifted()).toBe(true)

    clear()
    sync()
    expect(lifted()).toBe(true)

    clear()
    remove(s)
    expect(lifted()).toBe(true)
  })

  /* The rack asks the page which conversation is open. A page that has not
     wired that verb is a broken page, and the alternative -- filing everything
     under one key -- is exactly the bug this module exists to prevent. */
  it('refuses to guess the conversation when the page has not wired one', () => {
    window.RavenShell = { ...window.RavenShell!, sessionKey: undefined }
    expect(() => add(sheet())).toThrow('RavenShell.sessionKey is not wired')
  })
})
