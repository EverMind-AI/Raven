/** Tests for the desk palette's per-conversation open/shut memory. */

// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import * as palette from './palette'

afterEach(() => {
  palette._clearForTests()
  localStorage.clear()
})

describe('the palette memory', () => {
  /* The two screens are not one screen: a conversation is where the desk earns
     its place, and a draft is three empty lists over an empty composer. */
  it('shows the desk on a conversation and not on a draft', () => {
    expect(palette.read('s1')).toBe(true)
    expect(palette.read(null)).toBe(false)
  })

  it('remembers the collapse under the conversation it was made in', () => {
    palette.write('s1', false)

    expect(palette.read('s1')).toBe(false)
    /* And nowhere else. One global flag let the last conversation decide for
       every other one, which is the thing this replaces. */
    expect(palette.read('s2')).toBe(true)
  })

  it('remembers an open the reader asked for on a conversation they had collapsed', () => {
    palette.write('s1', false)
    palette.write('s1', true)

    expect(palette.read('s1')).toBe(true)
  })

  /* A preference the reader stated, not a record of what was on screen: it has
     to outlive the tab to mean what they meant by it, which is the one thing
     the layout notes in shell/persist.ts deliberately do not do. */
  it('stores the collapse where closing the tab cannot take it', () => {
    palette.write('s1', false)

    expect(JSON.parse(localStorage.getItem('raven.gui.desk.open') || '{}').s1.open).toBe(false)
    expect(sessionStorage.getItem('raven.gui.desk.open')).toBeNull()
  })

  /* An older build kept a single global boolean under this same name. */
  it('reads a scalar left by an older build as nothing stored', () => {
    localStorage.setItem('raven.gui.desk.open', 'true')

    expect(palette.read(null)).toBe(false)
    expect(palette.read('s1')).toBe(true)
    palette.write('s1', false)
    expect(palette.read('s1')).toBe(false)
  })

  it('does not carry unreadable storage into a decision', () => {
    localStorage.setItem('raven.gui.desk.open', '{ not json')

    expect(palette.read('s1')).toBe(true)
  })

  describe('a draft becoming a conversation', () => {
    /* The first message turns the draft into a session in place: same screen,
       same composer, an id where there was none. The desk the reader had just
       put away must not open in their face. */
    it("takes the draft's own answer with it", () => {
      palette.write(null, false)

      palette.adopt('s9')

      expect(palette.read('s9')).toBe(false)
    })

    it('leaves a conversation that has its own answer alone', () => {
      palette.write('s9', true)
      palette.write(null, false)

      palette.adopt('s9')

      expect(palette.read('s9')).toBe(true)
    })

    it('adopts once, so the next new conversation starts from the default', () => {
      palette.write(null, false)
      palette.adopt('s9')

      palette.adopt('s10')

      expect(palette.read('s10')).toBe(true)
    })

    /* The reader who never touched it on the draft said nothing to carry. */
    it('carries nothing when the draft was left as it was', () => {
      palette.adopt('s9')

      expect(palette.read('s9')).toBe(true)
    })
  })

  it('keeps the newest conversations when the store fills up', () => {
    for (let i = 0; i < 60; i += 1) palette.write(`s${i}`, false)

    expect(palette.read('s59')).toBe(false)
    /* Dropped, so it answers with the default rather than with a stale row. */
    expect(palette.read('s0')).toBe(true)
    expect(Object.keys(JSON.parse(localStorage.getItem('raven.gui.desk.open') || '{}')).length).toBe(50)
  })
})

/* Private mode, or over quota. `write_` swallows the refusal, and the question
   is what the reader gets for the rest of the visit. */
describe('when storage refuses the answer', () => {
  /* Defined on the instance and put back the same way. happy-dom's
     `localStorage` does not resolve `setItem` through `Storage.prototype`, so
     patching it there refuses nothing and every assertion below would pass on
     a write that went through -- which is why each test also checks the row
     really is absent. And the instance is a Proxy whose deleteProperty trap
     refuses, so the restore redefines rather than deletes. */
  const refusing = <T>(fn: () => T): T => {
    const real = localStorage.setItem
    Object.defineProperty(localStorage, 'setItem', {
      configurable: true,
      value: () => { throw new DOMException('quota', 'QuotaExceededError') },
    })
    try {
      return fn()
    } finally {
      Object.defineProperty(localStorage, 'setItem', { configurable: true, value: real })
    }
  }

  it('still answers with what the reader said', () => {
    refusing(() => palette.write('s1', false))

    expect(palette.read('s1')).toBe(false)
    /* And it really was refused -- otherwise this passes on the stored row and
       proves nothing. */
    expect(localStorage.getItem('raven.gui.desk.open')).toBeNull()
  })

  it('leaves a conversation it was never asked about on its default', () => {
    refusing(() => palette.write('s1', false))

    expect(palette.read('s2')).toBe(true)
  })

  /* A draft's answer is for a conversation with none of its own, and a refused
     write is still an answer. */
  it('does not hand a refused answer the draft answer instead', () => {
    palette.write(null, true)
    refusing(() => palette.write('s1', false))

    palette.adopt('s1')

    expect(palette.read('s1')).toBe(false)
  })
})
