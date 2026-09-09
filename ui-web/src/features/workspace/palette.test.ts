/** Tests for the desk palette's per-conversation open/shut memory: what the
 * reader stated, and null where they stated nothing. Who supplies the fallback
 * for null, and on what evidence, is `deskStore`'s -- see `deskUp` there.
 */

// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import * as palette from './palette'

afterEach(() => {
  palette._clearForTests()
  localStorage.clear()
})

describe('the palette memory', () => {
  /* Nothing stated is not the same as stated shut, and collapsing the two is
     what made an implicit default outlive the moment it was implied. The draft
     screen is the exception and keeps two values: it has no id to file under. */
  it('says nothing about a conversation the reader has not answered for', () => {
    expect(palette.stated('s1')).toBeNull()
    expect(palette.stated(null)).toBeNull()
  })

  it('remembers the collapse under the conversation it was made in', () => {
    palette.write('s1', false)

    expect(palette.stated('s1')).toBe(false)
    /* And nowhere else. One global flag let the last conversation decide for
       every other one, which is the thing this replaces. */
    expect(palette.stated('s2')).toBeNull()
  })

  it('remembers an open the reader asked for on a conversation they had collapsed', () => {
    palette.write('s1', false)
    palette.write('s1', true)

    expect(palette.stated('s1')).toBe(true)
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

    expect(palette.stated(null)).toBeNull()
    expect(palette.stated('s1')).toBeNull()
    palette.write('s1', false)
    expect(palette.stated('s1')).toBe(false)
  })

  it('does not carry unreadable storage into a decision', () => {
    localStorage.setItem('raven.gui.desk.open', '{ not json')

    expect(palette.stated('s1')).toBeNull()
  })

  describe('a draft becoming a conversation', () => {
    /* The first message turns the draft into a session in place: same screen,
       same composer, an id where there was none. The desk the reader had just
       put away must not open in their face. */
    it("takes the draft's own answer with it", () => {
      palette.write(null, false)

      palette.adopt('s9')

      expect(palette.stated('s9')).toBe(false)
    })

    it('leaves a conversation that has its own answer alone', () => {
      palette.write('s9', true)
      palette.write(null, false)

      palette.adopt('s9')

      expect(palette.stated('s9')).toBe(true)
    })

    it('adopts once, so the next new conversation is left unanswered', () => {
      palette.write(null, false)
      palette.adopt('s9')

      palette.adopt('s10')

      expect(palette.stated('s10')).toBeNull()
    })

    /* The reader who never touched it on the draft said nothing to carry, and
       nothing is what is recorded. Writing an implicit answer here instead was
       tried and is exactly what must not happen: it files a default as a
       preference, and the conversation then stays however it looked one second
       in -- across a switch away and back, and across a reload -- never
       reaching the answer a conversation with something in it should get. */
    it('records nothing when the draft was left as it was', () => {
      palette.adopt('s9')

      expect(palette.stated('s9')).toBeNull()
      expect(localStorage.getItem('raven.gui.desk.open')).toBeNull()
    })
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

    expect(palette.stated('s1')).toBe(false)
    /* And it really was refused -- otherwise this passes on the stored row and
       proves nothing. */
    expect(localStorage.getItem('raven.gui.desk.open')).toBeNull()
  })

  it('leaves a conversation it was never asked about unanswered', () => {
    refusing(() => palette.write('s1', false))

    expect(palette.stated('s2')).toBeNull()
  })

  /* A draft's answer is for a conversation with none of its own, and a refused
     write is still an answer. */
  it('does not hand a refused answer the draft answer instead', () => {
    palette.write(null, true)
    refusing(() => palette.write('s1', false))

    palette.adopt('s1')

    expect(palette.stated('s1')).toBe(false)
  })
})
