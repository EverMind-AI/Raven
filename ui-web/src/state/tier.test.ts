// @vitest-environment happy-dom
/* The sub-agent tier (state/tier.ts): what the catalogue the server answers
   with becomes on this side -- whether there is anything to offer, the rungs
   and their words, what the built-in ladder is called and where a rung goes --
   and how a pick moves. The row that draws it is the model picker's
   (features/model/ModelPicker.tsx, its own test); nothing here touches a
   document. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import * as session from '../lib/session'
import { resetSources, setSources } from './sources'
import * as tier from './tier'

import type { TierReply, TierSource } from './tier'

/* `raven/config/schema.py:_TIER_TEXTS` verbatim, which is what the server
   actually sends. Three sentences of this file's own invention would let the
   writer repeat one of them with the id swapped in and still pass -- that is
   the shape the retired copy had, and it is the only shape worth pinning. */
const MENU = [
  { id: 'medium', name: 'Medium', description: 'Faster and cheaper, for small, well-defined tasks.' },
  { id: 'high', name: 'High', description: 'The default: a balance of speed and quality.' },
  { id: 'max', name: 'Max', description: 'Deepest reasoning and full sub-agent effort, for complex or open-ended work.' },
]

let asked: Array<string | null>
let answer: (mode: string | null) => Promise<TierReply>

function source(): void {
  const src: TierSource = {
    read: () => { asked.push(null); return answer(null) },
    set: (mode) => { asked.push(mode); return answer(mode) },
  }
  setSources({ tier: src })
}

beforeEach(() => {
  setTranslator((key) => key)
  tier._resetForTests()
  session._resetForTests()
  session.setCurrent('cli:one')
  asked = []
  answer = async (mode) => ({ mode: mode ?? 'high', availableModes: MENU })
  source()
})

afterEach(() => {
  resetTranslator()
  tier._resetForTests()
  session._resetForTests()
  resetSources()
})

const names = (): string[] => tier.options().map((r) => r.name)
const ticked = (): string[] => tier.options().filter((r) => r.ticked).map((r) => r.id)

describe('the sub-agent tier', () => {
  it('offers nothing until the catalogue answers', async () => {
    /* There is no honest tier to draw before the reply. The one this side would
       invent is the built-in default, which is exactly the value a deployment
       with its own catalogue does not use. */
    tier.draw()
    expect(tier.offered()).toBe(false)

    await tier.load()

    expect(tier.offered()).toBe(true)
    expect(tier.label(tier.current())).toBe('gui.tier.high')
  })

  it('offers nothing for a build that offers no tiers', async () => {
    /* `"modes": {}` turns the surface off. An empty menu is a build with nothing
       to choose, not a build whose choice we failed to read. */
    answer = async () => ({ mode: null, availableModes: [] })

    await tier.load()

    expect(tier.offered()).toBe(false)
    expect(tier.options()).toEqual([])
  })

  it('reads the tier a draft first turn will run at, before there is a session', async () => {
    /* A draft is a conversation being written, and its first turn is as likely
       to dispatch sub-agents as any other. The handler answers the catalogue and
       its default for a key it has never seen, which is exactly that tier. Where
       a switch made here is WRITTEN is the seam's problem, not this module's --
       the live source stages it the way it already stages a model. */
    session.setCurrent(null)

    await tier.load()

    expect(asked).toEqual([null])
    expect(tier.offered()).toBe(true)
    expect(tier.current()).toBe('high')
  })

  it('offers nothing when the read fails, rather than naming a tier it does not know', async () => {
    answer = async () => { throw new Error('socket') }

    await tier.load()

    expect(tier.offered()).toBe(false)
  })

  it('offers one rung per tier the server lists, in the reader\'s words, with the catalogue\'s own sentence', async () => {
    await tier.load()

    /* The built-in three are named from the catalogue, because the server names
       them by capitalising the id and that is English whatever the page speaks. */
    expect(names()).toEqual(['gui.tier.medium', 'gui.tier.high', 'gui.tier.max'])
    expect(ticked()).toEqual(['high'])
    /* Every row against its OWN sentence, not one row against a substring: the
       rows carry one sentence per rung and a writer that hands them all the
       same one has to red here. A deployment's descriptions come through
       untouched, and raven translates the three it owns before they get here. */
    expect(tier.options().map((r) => r.sub)).toEqual(MENU.map((m) => m.description))
  })

  it('calls the built-in ladder a sub-agent tier, and says where it goes', async () => {
    await tier.load()

    expect(tier.isRanked()).toBe(true)
    expect(tier.scopeNote()).toBe('gui.tier.scope')
  })

  it('does not call a replacement catalogue a sub-agent tier', async () => {
    /* `clamp_tier` recognises `medium`/`high`/`max` and declines every other
       vocabulary, so a deployment's own rungs reach no sub-agent at all -- while
       what they DO move is the loop's own iteration cap and overlay. Heading a
       row of them "sub-agent effort" states the opposite of what picking one
       does. Same test as the clamp: every rung on offer, or none. */
    answer = async (mode) => ({
      mode: mode ?? 'thorough',
      availableModes: [{ id: 'quick', name: 'Quick' }, { id: 'thorough', name: 'Thorough' }],
    })

    await tier.load()

    expect(tier.isRanked()).toBe(false)
    expect(tier.scopeNote()).toBe('gui.tier.mode_scope')
  })

  it('does not call a catalogue that only partly spells the ladder a tier', async () => {
    /* Mixed, the clamp reads case by case, and a heading cannot be half true. */
    answer = async (mode) => ({
      mode: mode ?? 'high',
      availableModes: [{ id: 'high', name: 'High' }, { id: 'thorough', name: 'Thorough' }],
    })

    await tier.load()

    expect(tier.isRanked()).toBe(false)
  })

  it('says where the rung IN FORCE goes, which a mixed catalogue answers twice', async () => {
    /* `clamp_tier` honours an exact hit in any vocabulary that spells the rung
       the same way, so in `{high, thorough}` the `high` rung reaches sub-agents
       and `thorough` does not -- one catalogue, two answers. A note keyed to the
       catalogue said "not offered" over a rung that is. Verified against the
       clamp: `clamp_tier('high', ('medium','high','max'))` is `high`, and
       `clamp_tier('thorough', ...)` is `None`. */
    const mixed = [{ id: 'high', name: 'High' }, { id: 'thorough', name: 'Thorough' }]
    answer = async (mode) => ({ mode: mode ?? 'high', availableModes: mixed })

    await tier.load()

    // Still a mode catalogue by name...
    expect(tier.isRanked()).toBe(false)
    // ...but this rung does reach sub-agents, and the note has to say so.
    expect(tier.scopeNote()).toBe('gui.tier.scope')

    answer = async () => ({ mode: 'thorough', availableModes: mixed })
    await tier.load()

    expect(tier.scopeNote()).toBe('gui.tier.mode_scope')
  })

  it('names the rungs a replacement catalogue declares, not the built-in ladder', async () => {
    /* A deployment replaces the catalogue whole, and this control is not
       entitled to rename what it declares. */
    answer = async (mode) => ({
      mode: mode ?? 'thorough',
      availableModes: [{ id: 'quick', name: 'Quick' }, { id: 'thorough', name: 'Thorough' }],
    })

    await tier.load()

    expect(tier.label(tier.current())).toBe('Thorough')
    expect(names()).toEqual(['Quick', 'Thorough'])
  })

  it('drops a menu entry with no id, which is a row that could only misfire', async () => {
    /* The schema requires `id`, so this is about what arrives when something
       upstream is wrong rather than about a shape the contract allows. Such a
       row draws with an empty name and, clicked, asks the handler to switch to
       "" -- a refusal the reader cannot act on. It is not offered. */
    answer = async () => ({
      mode: 'high',
      availableModes: [{ id: 'high', name: 'High' }, { id: '', name: 'Broken' }] as typeof MENU,
    })

    await tier.load()

    expect(names()).toEqual(['gui.tier.high'])
  })

  it('takes the tier from the reply, not from the rung that was clicked', async () => {
    /* The handler decides. It clamps and it refuses, so painting the click first
       would show a tier the next turn will not run at. */
    await tier.load()
    answer = async () => ({ mode: 'high', availableModes: MENU })

    tier.pick('max')
    await vi.waitFor(() => expect(asked).toEqual([null, 'max']))

    expect(tier.current()).toBe('high')
    expect(ticked()).toEqual(['high'])
  })

  it('keeps the tier it had when the switch fails', async () => {
    await tier.load()
    answer = async () => { throw new Error('refused') }

    tier.pick('medium')
    await vi.waitFor(() => expect(asked).toEqual([null, 'medium']))

    expect(tier.current()).toBe('high')
    expect(tier.offered()).toBe(true)
  })

  it('asks for nothing when the rung already in force is picked', async () => {
    await tier.load()

    tier.pick('high')

    expect(asked).toEqual([null])
  })

  it('tells listeners only when the tier actually moved', async () => {
    /* Every read passes through the same place, including the one on each
       conversation change. A listener re-fetches on the news -- an instance pane
       showing what it inherits is one -- so announcing a value it already holds
       would have every open pane fetch for nothing. */
    const heard: string[] = []
    const off = tier.watch((next) => heard.push(next))
    try {
      await tier.load()
      expect(heard).toEqual(['high'])

      await tier.load()
      expect(heard).toEqual(['high'])

      answer = async () => ({ mode: 'max', availableModes: MENU })
      await tier.load()
      expect(heard).toEqual(['high', 'max'])
    } finally {
      off()
    }
  })
})
