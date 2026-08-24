// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { _resetForTests, add, dropClass, forget, remove, session, sync } from './sheets'
import { _resetForTests as sessionReset, setCurrent } from '../../shell/session'

import type { Shell } from '../../shell/bridge'

function wire(): void {
  const shell: Shell = {
    T: (key) => key,
    confirmAsk: () => {},
    showPage: () => {},
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
  sessionReset()
  setCurrent('a')
  _resetForTests()
  wire()
})

afterEach(() => {
  sessionReset()
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
    setCurrent(null)
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

    setCurrent('b')
    sync()
    expect(mounted()).toEqual([])
    expect(s.isConnected).toBe(false)

    setCurrent('a')
    sync()
    expect(rack().firstChild).toBe(s)
    expect((s.firstChild as HTMLInputElement).value).toBe('half typed')
  })

  it('mounts a sheet raised while the reader was away', () => {
    setCurrent('b')
    add(sheet(), 'a')
    setCurrent('a')
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
    setCurrent('b')
    sync()
    expect(mounted()).toEqual(['b'])
  })

  it('forgets a deleted conversation, mounted or not', () => {
    add(sheet())
    add(sheet(), 'b')
    forget('a')
    expect(mounted()).toEqual([])
    setCurrent('b')
    sync()
    expect(mounted()).toEqual(['b'])
    forget('b')
    expect(mounted()).toEqual([])
    /* Unmounting is not forgetting. Leaving the bucket behind would let the
       next sync put a deleted conversation's question back on screen, and the
       assertion above cannot tell the two apart on its own. */
    setCurrent('a')
    sync()
    expect(mounted()).toEqual([])
    setCurrent('b')
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

  /* The takedown a tenant registers on the way in. What makes it the rack's
     job and not the tenant's is the set of exits: a sheet leaves by being
     answered, by being replaced, or with its whole conversation -- and only
     this module is on the path of all three. */
  describe('the takedown a sheet registers', () => {
    it('runs when the sheet is removed, and only for that sheet', () => {
      const ran: string[] = []
      const a = sheet()
      const b = sheet()
      add(a, undefined, () => ran.push('a'))
      add(b, undefined, () => ran.push('b'))
      remove(a)
      expect(ran).toEqual(['a'])
    })

    it('runs when a class sweep retires the sheet', () => {
      const ran: string[] = []
      add(sheet('csheet'), undefined, () => ran.push('swept'))
      dropClass('csheet')
      expect(ran).toEqual(['swept'])
    })

    /* The exit a tenant cannot see. Deleting a conversation never reaches the
       sheet that was filed under it, so a tenant keeping its own book of
       takedowns leaves this one un-run -- which is what a document-level key
       handler outliving its question looks like. */
    it('runs when the conversation is forgotten, mounted or not', () => {
      const ran: string[] = []
      add(sheet(), 'a', () => ran.push('here'))
      add(sheet(), 'b', () => ran.push('away'))
      forget('a')
      expect(ran).toEqual(['here'])
      forget('b')
      expect(ran).toEqual(['here', 'away'])
    })

    /* A takedown that calls back into remove() -- which is the normal shape,
       since a tenant's close ends there -- must not run twice or recurse. */
    it('runs once, even when it removes the sheet again itself', () => {
      let ran = 0
      const s = sheet()
      add(s, undefined, () => {
        ran += 1
        remove(s)
      })
      remove(s)
      expect(ran).toBe(1)
    })

    /* Sheets without one are the common case: the dag card has nothing to
       unregister. */
    it('is optional', () => {
      const s = sheet()
      add(s)
      expect(() => remove(s)).not.toThrow()
    })
  })
})
