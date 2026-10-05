import { afterEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { KNOWN_KINDS, isKnownKind, kindClass, kindLabel, kindSlug } from './palette'

afterEach(() => {
  resetTranslator()
})

describe('the kind palette', () => {
  it('gives each of the seven conversation kinds a class and a catalogue word', () => {
    setTranslator((key) => key)
    expect(KNOWN_KINDS).toHaveLength(7)
    expect(kindClass('llm.input')).toBe('trajectory-k-llm-input')
    expect(kindSlug('tool.output')).toBe('tool-output')
    expect(kindLabel('agent.reply')).toBe('gui.trajectory.kind.agent_reply')
    for (const kind of KNOWN_KINDS) expect(isKnownKind(kind)).toBe(true)
  })

  it('folds an unknown kind onto the one grey and keeps its own name', () => {
    setTranslator(() => 'translated')
    expect(isKnownKind('browser.output')).toBe(false)
    expect(kindClass('browser.output')).toBe('trajectory-k-other')
    expect(kindClass('span.error')).toBe('trajectory-k-other')
    expect(kindLabel('browser.output')).toBe('browser.output')
  })
})
