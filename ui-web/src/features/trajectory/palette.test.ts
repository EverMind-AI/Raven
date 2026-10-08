// @ts-expect-error Vitest provides Node built-ins without adding Node types to the browser bundle.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { KNOWN_KINDS, NAMED_KINDS, kindClass, kindKey, kindLabel, kindLabels, kindSlug } from './palette'

const catalogue = (): Record<string, { en: string; zh: string }> =>
  (JSON.parse(readFileSync('../i18n/messages.json', 'utf8') as string) as { ui: Record<string, { en: string; zh: string }> }).ui

/* Every kind the index can emit, by the slot rules in raven/trajectory/entries.py
   and the block registry in raven/trajectory/details.py. */
const EMITTED = [
  'user.input', 'turn.end', 'llm.input', 'llm.thinking', 'llm.output', 'tool.input', 'tool.output',
  'skill.read', 'skill.inject', 'skill.rewrite.input', 'skill.rewrite.output', 'skill.gate.input', 'skill.gate.output',
  'context.curate.input', 'context.curate.output', 'memory.recall', 'memory.store', 'memory.feedback.summary',
  'memory.enqueue.summary', 'memory.extract.summary', 'memory.profile_refresh.summary', 'memory.consolidate.summary',
  'personalize.classify.input', 'personalize.classify.output', 'personalize.question.input', 'personalize.postlearn.output',
  'subagent.run', 'subagent.external.transcript', 'span.error', 'span.malformed', 'span.unreadable', 'plugin.load.summary',
  'foo.bar.summary',
]

describe('the kind palette', () => {
  it('has a word in both languages for every kind the index emits, each Chinese word at most four characters', () => {
    const ui = catalogue()
    for (const kind of EMITTED) {
      const key = kindKey(kind)
      expect(key, kind).not.toBeNull()
      const entry = ui[key as string]
      expect(entry, key as string).toBeTruthy()
      expect(entry!.en.length, key as string).toBeGreaterThan(0)
      expect([...entry!.zh].length, `${key}: ${entry!.zh}`).toBeLessThanOrEqual(4)
    }
    for (const kind of NAMED_KINDS) expect(ui[kindKey(kind) as string], kind).toBeTruthy()
    expect(kindKey('artifact:custom')).toBeNull()
  })

  it('keeps the seven conversation kinds coloured and everything else grey', () => {
    expect(KNOWN_KINDS).toHaveLength(7)
    expect(kindSlug('tool.output')).toBe('tool-output')
    expect(kindClass('memory.recall')).toBe('trajectory-k-other')
    expect(kindSlug('personalize.classify.input')).toBe('other')
  })

  it('labels through the translator, naming the raw kind only where it has no word', () => {
    setTranslator((key) => `<${key}>`)
    try {
      expect(kindLabel('memory.recall')).toBe('<gui.trajectory.kind.memory_recall>')
      expect(kindLabel('personalize.extract.output')).toBe('<gui.trajectory.kind.personalize_output>')
      expect(kindLabel('subagent.external.usage')).toBe('<gui.trajectory.kind.subagent_external>')
      expect(kindLabel('weird.thing.summary')).toBe('<gui.trajectory.kind.summary>')
      expect(kindLabel('artifact:custom')).toBe('artifact:custom')
      expect(new Set(kindLabels()).size).toBe(kindLabels().length)
      expect(kindLabels()).toContain('<gui.trajectory.kind.summary>')
    } finally {
      resetTranslator()
    }
  })
})
