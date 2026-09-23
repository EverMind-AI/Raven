import { describe, expect, it } from 'vitest'

import { KIND_GLYPH, KIND_LABEL, guessKind, modelKind, sameModel } from './types'
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

/* What `providers/wire.py`'s merge_key answers for the same pairs, measured
   against it rather than read off its source. A provider's prefixes are its own
   name AND every name it used to answer to (`ProviderSpec.route_names`), which
   is how a model id written before a rename still names the model it named
   then; a prefix belonging to some other provider is not one of them.

   A literal copy, like the kind table above: the point is that a change to
   either side shows up as a disagreement, which a shared fixture would hide. */
const IDENTITY: Array<[string, string[], string, string, boolean]> = [
  ['zai', ['zai', 'zhipu'], 'zhipu/glm-4.6', 'zai/glm-4.6', true],
  ['zai', ['zai', 'zhipu'], 'zhipu/glm-4.6', 'glm-4.6', true],
  ['zai', ['zai', 'zhipu'], 'zai/glm-4.6', 'glm-4.6', true],
  ['zai', ['zai', 'zhipu'], 'ZHIPU/GLM-4.6', 'zai/glm-4.6', true],
  ['zai', ['zai', 'zhipu'], 'zhipu/glm-4.6', 'zhipu/glm-4.5', false],
  ['anthropic', ['anthropic'], 'anthropic/claude-opus-5', 'claude-opus-5', true],
  ['anthropic', ['anthropic'], 'openrouter/claude-opus-5', 'claude-opus-5', false],
]

describe('one model, however it was spelled', () => {
  it.each(IDENTITY)('%s: %j says %s and %s are the same model: %s', (id, routes, a, b, same) => {
    expect(sameModel({ id, routes }, a, b)).toBe(same)
  })

  it('falls back to the provider name alone when nothing says what it answers to', () => {
    /* A row from a wire that does not carry them, and every offline fixture.
       The current name is always one of the prefixes, so this is the old
       behaviour rather than no behaviour. */
    expect(sameModel({ id: 'zai' }, 'zai/glm-4.6', 'glm-4.6')).toBe(true)
    expect(sameModel({ id: 'zai' }, 'zhipu/glm-4.6', 'glm-4.6')).toBe(false)
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
