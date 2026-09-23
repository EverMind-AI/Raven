// @vitest-environment happy-dom
/* Whether the add-a-model popover can see that a model is already on the
 * provider.
 *
 * `added` is the wire's own answer, computed with `merge_key`; the fallback
 * below is for a row the vendor's list never named, which arrives with no
 * answer at all. That fallback is an identity question, so it has to ask the
 * one the backend answers -- including the prefixes a provider used to answer
 * to, which the row carries (`features/model/types.ts`'s `ModelHost`).
 */
import { describe, expect, it } from 'vitest'

import { isAdded } from './AddModelPop'

import type { ModelCandidate } from '../types'

const row = (id: string): ModelCandidate => ({ id, label: id, kind: 'text', added: false })

describe('a model the vendor list did not name', () => {
  it('reads as added when the configured list spells it the same way', () => {
    expect(isAdded({ id: 'zai' }, row('zai/glm-4.6'), ['glm-4.6'])).toBe(true)
  })

  it('reads as added when it is spelled with a name the provider used to answer to', () => {
    /* A model id written before the rename, still in the config section. Asking
       with the current name alone reported it as missing, and the popover
       offered to add a model that was already there. */
    expect(isAdded({ id: 'zai', routes: ['zai', 'zhipu'] }, row('zai/glm-4.6'), ['zhipu/glm-4.6'])).toBe(true)
  })

  it('does not read a different model as added', () => {
    expect(isAdded({ id: 'zai', routes: ['zai', 'zhipu'] }, row('zai/glm-4.5'), ['zhipu/glm-4.6'])).toBe(false)
  })

  it('takes the wire\'s own answer whatever the spellings', () => {
    expect(isAdded({ id: 'zai' }, { ...row('anything'), added: true }, [])).toBe(true)
  })
})
