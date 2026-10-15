/* Whether the open conversation is an Imported Conversation, read off its key. */
import { afterEach, describe, expect, it } from 'vitest'

import { isImportedKey, readingImported } from './imported'
import { _resetForTests, setCurrent } from './session'

afterEach(() => _resetForTests())

describe('an imported key', () => {
  it('is one that starts with the imported channel', () => {
    expect(isImportedKey('imported:' + 'a'.repeat(32))).toBe(true)
    expect(isImportedKey('imported:anything')).toBe(true)
    for (const key of ['tui:abc', 'cron:imported:x', 'imported', '', null, undefined]) {
      expect(isImportedKey(key), String(key)).toBe(false)
    }
  })

  it('makes the conversation open now read-only, and only while it is open', () => {
    expect(readingImported()).toBe(false)
    setCurrent('imported:' + 'a'.repeat(32))
    expect(readingImported()).toBe(true)
    setCurrent('tui:abc')
    expect(readingImported()).toBe(false)
    setCurrent(null)
    expect(readingImported()).toBe(false)
  })
})
