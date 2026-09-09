// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as session from './session'
import * as tier from './tier'

import type { Shell } from './bridge'
import type { TierReply, TierSource } from './tier'

/* The same nodes page.html carries, in the same nesting: the chip holds the icon
   slot and the label and ships `hidden`, the panel holds the list. */
function markup(): void {
  document.body.innerHTML = `
    <div class="card">
      <button class="chip" id="tierChip" aria-expanded="false" aria-haspopup="true" hidden>
        <svg class="pico" viewBox="0 0 24 24" aria-hidden="true"></svg>
        <span id="tierName"></span>
      </button>
      <div class="pop" id="tierPop" data-open="false">
        <div class="hd"><span class="lab"></span></div>
        <div id="tierList" role="radiogroup"></div>
        <div class="note"></div>
      </div>
    </div>`
}

/* `raven/config/schema.py:_TIER_TEXTS` verbatim, which is what the server
   actually sends. Three sentences of this file's own invention would let the
   writer repeat one of them with the id swapped in and still pass -- that is
   the shape the retired copy had, and it is the only shape worth pinning. */
const MENU = [
  { id: 'medium', name: 'Medium', description: 'The least effort a sub-agent is asked for.' },
  { id: 'high', name: 'High', description: 'The middle amount of effort, between the other two.' },
  { id: 'max', name: 'Max', description: 'The most effort a sub-agent is asked for.' },
]

let asked: Array<string | null>
let answer: (mode: string | null) => Promise<TierReply>

function source(): void {
  const src: TierSource = {
    read: () => { asked.push(null); return answer(null) },
    set: (mode) => { asked.push(mode); return answer(mode) },
  }
  window.DS = { tier: src } as unknown as typeof window.DS
}

beforeEach(() => {
  window.RavenShell = { T: (key) => key, confirmAsk: () => {}, showPage: () => {} } as Shell
  markup()
  tier._resetForTests()
  session._resetForTests()
  session.setCurrent('cli:one')
  asked = []
  answer = async (mode) => ({ mode: mode ?? 'high', availableModes: MENU })
  source()
})

afterEach(() => {
  delete window.RavenShell
  document.body.innerHTML = ''
  tier._resetForTests()
  session._resetForTests()
})

const chip = (): HTMLElement => document.getElementById('tierChip')!
const pop = (): HTMLElement => document.getElementById('tierPop')!
const name = (): string => document.getElementById('tierName')!.textContent || ''
const rows = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('#tierList .prow')]

describe('the sub-agent tier chip', () => {
  it('stays hidden until the catalogue answers', async () => {
    /* There is no honest tier to draw before the reply. The one the chip would
       invent is the built-in default, which is exactly the value a deployment
       with its own catalogue does not use. */
    tier.draw()
    expect(chip().hidden).toBe(true)

    await tier.load()

    expect(chip().hidden).toBe(false)
    expect(name()).toBe('High')
  })

  it('stays hidden for a build that offers no tiers', async () => {
    /* `"modes": {}` turns the surface off. An empty menu is a build with nothing
       to choose, not a build whose choice we failed to read. */
    answer = async () => ({ mode: null, availableModes: [] })

    await tier.load()

    expect(chip().hidden).toBe(true)
  })

  it('shows the tier a draft first turn will run at, before there is a session', async () => {
    /* A draft is a conversation being written, and its first turn is as likely
       to dispatch sub-agents as any other. The handler answers the catalogue and
       its default for a key it has never seen, which is exactly that tier. Where
       a switch made here is WRITTEN is the seam's problem, not this module's --
       the live source stages it the way it already stages a model. */
    session.setCurrent(null)

    await tier.load()

    expect(asked).toEqual([null])
    expect(chip().hidden).toBe(false)
    expect(name()).toBe('High')
  })

  it('stays hidden when the read fails, rather than naming a tier it does not know', async () => {
    answer = async () => { throw new Error('socket') }

    await tier.load()

    expect(chip().hidden).toBe(true)
  })

  it('draws one row per tier the server offers, with the catalogue own text', async () => {
    await tier.load()
    tier.open()

    expect(rows().map((r) => r.querySelector('.nm')!.textContent)).toEqual(['Medium', 'High', 'Max'])
    expect(rows()[1]!.getAttribute('aria-checked')).toBe('true')
    expect(rows()[0]!.getAttribute('aria-checked')).toBe('false')
    /* Every row against its OWN sentence, not one row against a substring: the
       rows carry one sentence per rung and a writer that hands them all the
       same one has to red here. Both places it lands, too -- the line the
       reader sees, and the title for one too long for the line. A deployment's
       descriptions come through untouched, and raven translates the three it
       owns before they get here. */
    expect(rows().map((r) => r.querySelector('.sub')!.textContent)).toEqual(MENU.map((m) => m.description))
    expect(rows().map((r) => r.title)).toEqual(MENU.map((m) => m.description))
    expect(pop().dataset.open).toBe('true')
  })

  it('calls the built-in ladder a sub-agent tier, and says where it goes', async () => {
    await tier.load()
    tier.open()

    expect(document.querySelector('#tierPop .hd .lab')!.textContent).toBe('gui.tier.title')
    expect(document.querySelector('#tierPop .note')!.textContent).toBe('gui.tier.scope')
    expect(chip().getAttribute('aria-label')).toBe('gui.tier.title: High')
  })

  it('does not call a replacement catalogue a sub-agent tier', async () => {
    /* `clamp_tier` recognises `medium`/`high`/`max` and declines every other
       vocabulary, so a deployment's own rungs reach no sub-agent at all -- while
       what they DO move is the loop's own iteration cap and overlay. Heading a
       panel of them "sub-agent effort" states the opposite of what picking one
       does. Same test as the clamp: every rung on offer, or none. */
    answer = async (mode) => ({
      mode: mode ?? 'thorough',
      availableModes: [{ id: 'quick', name: 'Quick' }, { id: 'thorough', name: 'Thorough' }],
    })

    await tier.load()
    tier.open()

    expect(document.querySelector('#tierPop .hd .lab')!.textContent).toBe('gui.tier.mode')
    expect(document.querySelector('#tierPop .note')!.textContent).toBe('gui.tier.mode_scope')
    expect(chip().getAttribute('aria-label')).toBe('gui.tier.mode: Thorough')
  })

  it('does not call a catalogue that only partly spells the ladder a tier', async () => {
    /* Mixed, the clamp reads case by case, and a heading cannot be half true. */
    answer = async (mode) => ({
      mode: mode ?? 'high',
      availableModes: [{ id: 'high', name: 'High' }, { id: 'thorough', name: 'Thorough' }],
    })

    await tier.load()
    tier.open()

    expect(document.querySelector('#tierPop .hd .lab')!.textContent).toBe('gui.tier.mode')
  })

  it('says where the rung IN FORCE goes, which a mixed catalogue answers twice', async () => {
    /* `clamp_tier` honours an exact hit in any vocabulary that spells the rung
       the same way, so in `{high, thorough}` the `high` rung reaches sub-agents
       and `thorough` does not -- one catalogue, two answers. A footer keyed to
       the catalogue said "not offered" over a rung that is. Verified against the
       clamp: `clamp_tier('high', ('medium','high','max'))` is `high`, and
       `clamp_tier('thorough', ...)` is `None`. */
    const mixed = [{ id: 'high', name: 'High' }, { id: 'thorough', name: 'Thorough' }]
    answer = async (mode) => ({ mode: mode ?? 'high', availableModes: mixed })

    await tier.load()
    tier.open()

    // Still a mode catalogue by name...
    expect(document.querySelector('#tierPop .hd .lab')!.textContent).toBe('gui.tier.mode')
    // ...but this rung does reach sub-agents, and the footer has to say so.
    expect(document.querySelector('#tierPop .note')!.textContent).toBe('gui.tier.scope')

    answer = async () => ({ mode: 'thorough', availableModes: mixed })
    await tier.load()
    tier.open()

    expect(document.querySelector('#tierPop .note')!.textContent).toBe('gui.tier.mode_scope')
  })

  it('keeps the footer honest when the whole catalogue is the ladder', async () => {
    await tier.load()
    tier.open()

    expect(document.querySelector('#tierPop .note')!.textContent).toBe('gui.tier.scope')
  })

  it('names the rungs a replacement catalogue declares, not the built-in ladder', async () => {
    /* A deployment replaces the catalogue whole, and this control is not
       entitled to rename what it declares. */
    answer = async (mode) => ({
      mode: mode ?? 'thorough',
      availableModes: [{ id: 'quick', name: 'Quick' }, { id: 'thorough', name: 'Thorough' }],
    })

    await tier.load()
    tier.open()

    expect(name()).toBe('Thorough')
    expect(rows().map((r) => r.querySelector('.nm')!.textContent)).toEqual(['Quick', 'Thorough'])
  })

  it('drops a menu entry with no id, which is a row that could only misfire', () => {
    /* The schema requires `id`, so this is about what arrives when something
       upstream is wrong rather than about a shape the contract allows. Such a
       row draws with an empty name and, clicked, asks the handler to switch to
       "" -- a refusal the reader cannot act on. It is not offered. */
    answer = async () => ({
      mode: 'high',
      availableModes: [{ id: 'high', name: 'High' }, { id: '', name: 'Broken' }] as typeof MENU,
    })

    return tier.load().then(() => {
      tier.open()
      expect(rows().map((r) => r.querySelector('.nm')!.textContent)).toEqual(['High'])
    })
  })

  it('takes the tier from the reply, not from the row that was clicked', async () => {
    /* The handler decides. It clamps and it refuses, so painting the click first
       would show a tier the next turn will not run at. */
    await tier.load()
    answer = async () => ({ mode: 'high', availableModes: MENU })
    tier.open()

    rows()[2]!.click()
    await vi.waitFor(() => expect(asked).toEqual([null, 'max']))

    expect(tier.current()).toBe('high')
    expect(name()).toBe('High')
  })

  it('keeps the tier it had when the switch fails', async () => {
    await tier.load()
    answer = async () => { throw new Error('refused') }
    tier.open()

    rows()[0]!.click()
    await vi.waitFor(() => expect(asked).toEqual([null, 'medium']))

    expect(tier.current()).toBe('high')
    expect(chip().hidden).toBe(false)
    expect(name()).toBe('High')
  })

  it('asks for nothing when the row already in force is clicked', async () => {
    await tier.load()
    tier.open()

    rows()[1]!.click()

    expect(asked).toEqual([null])
    expect(pop().dataset.open).toBe('false')
  })

  it('closes on the chip a second time, since the panel has no close of its own', async () => {
    await tier.load()

    tier.toggle()
    expect(tier.isOpen()).toBe(true)
    expect(chip().getAttribute('aria-expanded')).toBe('true')

    tier.toggle()
    expect(tier.isOpen()).toBe(false)
    expect(chip().getAttribute('aria-expanded')).toBe('false')
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

  it('reparents the panel to the body, or it is positioned against the wrong box', async () => {
    /* The composer card's entrance animation makes it a containing block, which
       re-bases `position: fixed` inside it -- the same trap `perm.ts` records. */
    await tier.load()
    expect(pop().parentElement).not.toBe(document.body)

    tier.open()

    expect(pop().parentElement).toBe(document.body)
    expect(pop().style.position).toBe('fixed')
  })
})
