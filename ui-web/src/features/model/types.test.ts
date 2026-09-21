import { describe, expect, it } from 'vitest'

import { KIND_GLYPH, KIND_LABEL, guessKind, modelKind } from './types'
import { KIND_ORDER } from './types'

import type { Kind } from './types'

/* The same nine rows as tests/test_provider_registry_data.py's NAME_GUESSES.
   Two literal copies rather than a shared fixture: the point is that a change
   to either side shows up as a disagreement, which a shared file would hide. */
const TABLE: Array<[string, Kind]> = [
  ['openai/text-embedding-3-small', 'embedding'],
  ['BAAI/bge-reranker-v2-m3', 'reranker'],
  ['my-team/bge-reranker-custom', 'reranker'],
  ['jina-embeddings-v3', 'embedding'],
  ['bge-m3', 'embedding'],
  ['gte-large', 'embedding'],
  ['rerank-co/gpt-4', 'text'],
  ['google/gemini-2.5-flash-image', 'text'],
  ['deepseek-v4-pro', 'text'],
]

describe('guessing a kind from an id alone', () => {
  it.each(TABLE)('files %s as %s, the way registry_data.inferred_tags does', (id, kind) => {
    expect(guessKind(id)).toBe(kind)
  })
})

describe('reading the kind the registry sent', () => {
  it('files a model with no label, or a kind this page has never heard of, under text', () => {
    expect(modelKind(undefined)).toBe('text')
    expect(modelKind({})).toBe('text')
    expect(modelKind({ kind: 'holographic' })).toBe('text')
  })

  it('passes through every kind the wire may send', () => {
    for (const kind of KIND_ORDER) expect(modelKind({ kind })).toBe(kind)
  })
})

describe('the two tables', () => {
  it('name a label and a sprite id for every kind', () => {
    for (const kind of KIND_ORDER) {
      expect(KIND_LABEL[kind]).toMatch(/^gui\.model\.type\./)
      expect(KIND_GLYPH[kind]).toBeTruthy()
    }
  })
})
