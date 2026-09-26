// @vitest-environment happy-dom
/* The persona page: describing one, keeping it, and the wall of what was kept.
 *
 * Three things here are the point of the page and nothing else asserts them.
 *
 * Describing leaves a DRAFT -- the turn writes nothing, so the wall is what a
 * reader decided to keep rather than a row per turn.
 *
 * The maker is a CONVERSATION. Designing a Persona takes more than one
 * sentence: the engine asks back, and the reader refines. Both were broken
 * when this page was a single send -- the question went to a composer sheet
 * behind the page and the maker waited out a draft that could not arrive.
 *
 * And starting one stages rather than creates: the click leaves a Harness on
 * the draft conversation (state/harness.ts) and a closed page, because the
 * conversation itself is not made until the first message promotes it.
 */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { claimantFor, _resetForTests as resetClaims } from '../../state/clarifyClaim'
import { _resetForTests as resetHarness, staged } from '../../state/harness'
import * as page from '../../state/page'
import { resetSources, sources } from '../../state/sources'
import { PersonaApp } from './PersonaPage'
import * as store from './store'

import type { PersonaLine, PersonaRow, PersonaSource, PersonaStep } from './types'

function row(name: string, over: Partial<PersonaRow> = {}): PersonaRow {
  return {
    name,
    description: `${name} description`,
    artifact_kind: 'harness',
    coordinator: true,
    coordinator_brief: `${name} coordinator brief`,
    workers: [{ label: 'planner', agent: 'Raven-Research' }],
    origin: 'user',
    disabled: false,
    error: '',
    ...over,
  }
}

/* The draft button the start reaches for, which is the page's own in the real
   shell (chrome/Rail.tsx). Absent in the one case below, which is what "this
   build cannot start one" means now the staging itself cannot fail. */
function withDraftButton(): void {
  const button = document.createElement('button')
  button.id = 'newBtn'
  document.body.appendChild(button)
}

const showPage = vi.spyOn(page, 'show')

interface Stub {
  drafts: (PersonaRow | null)[]
  lines: PersonaLine[]
  described: { session: string; text: string }[]
  saved: { session: string; name?: string }[]
  discarded: string[]
  removed: string[]
  /** Runs inside `describe`, which is where a real turn asks its question. */
  onDescribe?: (session: string) => void
  /** Pushes a step the way the turn's own frames would. */
  step?: (step: PersonaStep) => void
  unwatched?: boolean
}

function install(rows: PersonaRow[], stub: Partial<Stub> = {}): Stub {
  const state: Stub = { drafts: [], lines: [], described: [], saved: [], discarded: [], removed: [], ...stub }
  const source: PersonaSource = {
    list: async () => rows,
    openMaker: async () => 'tui:maker',
    describe: async (session, text) => {
      state.described.push({ session, text })
      state.onDescribe?.(session)
    },
    history: async () => state.lines,
    watch: async (_session, onStep) => {
      state.step = onStep
      return () => { state.unwatched = true }
    },
    draft: async () => (state.drafts.length > 1 ? state.drafts.shift()! : state.drafts[0] ?? null),
    save: async (session, name) => { state.saved.push({ session, name }); return name || 'made-one' },
    discard: async (session) => { state.discarded.push(session) },
    remove: async (name) => { state.removed.push(name) },
  }
  sources.persona = source
  return state
}

beforeEach(() => {
  setTranslator((key) => key)
  store._resetForTests()
  resetClaims()
  resetHarness()
  showPage.mockClear().mockImplementation(() => {})
})

afterEach(() => {
  cleanup()
  resetSources()
  resetTranslator()
  vi.useRealTimers()
  document.getElementById('newBtn')?.remove()
})

describe('making a persona', () => {
  it('opens on the maker, not on the wall', () => {
    install([])
    render(<PersonaApp />)
    expect(screen.getByLabelText('gui.persona.ask')).toBeTruthy()
    expect(screen.queryByLabelText('gui.persona.search')).toBeNull()
  })

  it('describes one, and shows what came back without saving it', async () => {
    const stub = install([], { drafts: [row('nightly-captain')] })
    render(<PersonaApp />)

    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'watch the release' } })
    await act(async () => { await store.send() })

    expect(stub.described).toEqual([{ session: 'tui:maker', text: 'watch the release' }])
    expect(stub.saved).toEqual([])
    expect(screen.getByText('gui.persona.draft_head')).toBeTruthy()
    /* The name is offered, because the reader is naming a thing they will look
       for later and the generator only guessed. */
    expect((screen.getByLabelText('gui.persona.draft_name') as HTMLInputElement).value).toBe('nightly-captain')
  })

  it('draws the reader’s own line before the turn answers', async () => {
    install([], { drafts: [row('nightly-captain')] })
    render(<PersonaApp />)
    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'watch the release' } })
    await act(async () => { await store.send() })

    expect(screen.getByText('watch the release')).toBeTruthy()
    expect(store.get().text).toBe('')
  })

  it('shows the question the engine asks, on the page, and answers it there', async () => {
    let answered: string | null = null
    const stub = install([], {
      drafts: [null],
      onDescribe: (session) => {
        claimantFor(session)!.ask(
          { requestId: 'q1', question: 'There is already one by that name -- what now?', choices: ['Save it as v2', 'Replace the old one'] },
          (text) => { answered = text },
        )
      },
    })
    render(<PersonaApp />)
    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'travel concierge' } })
    await act(async () => { await store.send() })

    /* The page stops waiting the moment the turn blocks on the reader: a
       spinner over a question nobody can see is the bug this seam exists for. */
    expect(store.get().busy).toBe(false)
    expect(screen.getByText('There is already one by that name -- what now?')).toBeTruthy()

    stub.drafts = [row('travel-concierge-v2')]
    await act(async () => { fireEvent.click(screen.getByText('Save it as v2')) })
    await act(async () => { await Promise.resolve() })

    expect(answered).toBe('Save it as v2')
    expect(store.get().ask).toBeNull()
  })

  it('says what the turn is doing, and what it has run', async () => {
    const stub = install([], { drafts: [null] })
    render(<PersonaApp />)
    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'watch the release' } })

    await act(async () => {
      const sending = store.send()
      /* The frames the turn really pushes: the page read as frozen on a
         two-minute generation while it drew one static line. */
      await Promise.resolve()
      stub.step?.({ kind: 'tool', label: 'create_persona_playbook' })
      stub.drafts = [row('nightly-captain')]
      await sending
    })

    expect(stub.unwatched).toBeUndefined()
    expect(store.get().ran).toEqual(['create_persona_playbook'])
  })

  it('keeps the conversation, so a second sentence refines the same one', async () => {
    const stub = install([], { drafts: [row('nightly-captain')] })
    render(<PersonaApp />)
    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'watch the release' } })
    await act(async () => { await store.send() })

    fireEvent.change(screen.getByLabelText('gui.persona.reply'), { target: { value: 'fewer workers' } })
    await act(async () => { await store.send() })

    /* One conversation, not two: the refinement is about the Persona just
       described, and a fresh session would have thrown that away. */
    expect(stub.described.map((d) => d.session)).toEqual(['tui:maker', 'tui:maker'])
    expect(stub.described[1]!.text).toBe('fewer workers')
  })

  it('keeps it under the name the reader chose, and lands on the wall', async () => {
    const stub = install([row('nightly-captain')], { drafts: [row('nightly-captain')] })
    render(<PersonaApp />)
    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'watch the release' } })
    await act(async () => { await store.send() })

    fireEvent.change(screen.getByLabelText('gui.persona.draft_name'), { target: { value: 'release-captain' } })
    await act(async () => { await store.save() })

    expect(stub.saved).toEqual([{ session: 'tui:maker', name: 'release-captain' }])
    expect(store.get().view).toBe('saved')
    expect(store.get().draft).toBeNull()
    expect(await screen.findByText('nightly-captain')).toBeTruthy()
  })

  it('lets one go without saving it', async () => {
    const stub = install([], { drafts: [row('nightly-captain')] })
    render(<PersonaApp />)
    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'watch the release' } })
    await act(async () => { await store.send() })

    await act(async () => { await store.discard() })

    expect(stub.discarded).toEqual(['tui:maker'])
    expect(stub.saved).toEqual([])
    expect(screen.queryByText('gui.persona.draft_head')).toBeNull()
  })

  it('starts over on an empty conversation, and gives the questions back', async () => {
    install([], { drafts: [row('nightly-captain')] })
    render(<PersonaApp />)
    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'watch the release' } })
    await act(async () => { await store.send() })

    await act(async () => { store.reset() })

    expect(store.get().lines).toEqual([])
    expect(store.get().session).toBe('')
    expect(claimantFor('tui:maker')).toBeUndefined()
  })

  it('hands the questions back when the reader leaves, and takes them again on return', async () => {
    install([], { drafts: [null], onDescribe: () => {} })
    render(<PersonaApp />)
    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'watch the release' } })
    await act(async () => {
      const sending = store.send()
      await Promise.resolve()
      store.get().session && claimantFor('tui:maker')
      vi.useFakeTimers()
      await vi.advanceTimersByTimeAsync(store.POLL_MS * store.POLL_TRIES + store.POLL_MS)
      await sending
      vi.useRealTimers()
    })
    expect(claimantFor('tui:maker')).toBeTruthy()

    /* A claim held by a page nobody is looking at is a question asked where it
       cannot be answered: the sheet never opens and the turn waits it out. */
    act(() => { store.closePage() })
    expect(claimantFor('tui:maker')).toBeUndefined()

    act(() => { store.openPage() })
    expect(claimantFor('tui:maker')).toBeTruthy()
  })

  it('clears what was typed once it has been sent as an answer', async () => {
    let answered: string | null = null
    install([], {
      drafts: [row('nightly-captain')],
      onDescribe: (session) => {
        claimantFor(session)!.ask({ requestId: 'q1', question: 'Which one?', choices: [] }, (t) => { answered = t })
      },
    })
    render(<PersonaApp />)
    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'travel concierge' } })
    await act(async () => { await store.send() })

    fireEvent.change(screen.getByLabelText('gui.persona.reply'), { target: { value: 'Beijing' } })
    await act(async () => { await store.answer('Beijing') })

    expect(answered).toBe('Beijing')
    /* Left in the box, the answer is one click from being sent again as a
       fresh describe turn. */
    expect(store.get().text).toBe('')
  })

  it('says so when the turn leaves nothing in the time the page waits', async () => {
    vi.useFakeTimers()
    install([], { drafts: [null] })
    render(<PersonaApp />)
    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'watch the release' } })

    await act(async () => {
      const sending = store.send()
      await vi.advanceTimersByTimeAsync(store.POLL_MS * store.POLL_TRIES + store.POLL_MS)
      await sending
    })

    expect(store.get().busy).toBe(false)
    expect(store.get().makeErr).toBe('gui.persona.slow')
  })

  it('picks up a draft that landed after the page stopped waiting', async () => {
    vi.useFakeTimers()
    const stub = install([], { drafts: [null] })
    render(<PersonaApp />)
    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'watch the release' } })
    await act(async () => {
      const sending = store.send()
      await vi.advanceTimersByTimeAsync(store.POLL_MS * store.POLL_TRIES + store.POLL_MS)
      await sending
    })
    expect(store.get().makeErr).toBe('gui.persona.slow')

    /* The turn was slower than the page, not broken: measured at 113s against
       a 90s budget once, which is what this whole affordance is for. */
    stub.drafts = [row('nightly-captain')]
    await act(async () => { await store.recheck() })

    expect(store.get().draft?.name).toBe('nightly-captain')
    expect(store.get().makeErr).toBe('')
  })

  it('reports a refused describe instead of waiting on a turn that never ran', async () => {
    install([])
    sources.persona = { ...sources.persona!, describe: async () => { throw new Error('no engine') } }
    render(<PersonaApp />)
    fireEvent.change(screen.getByLabelText('gui.persona.ask'), { target: { value: 'watch the release' } })

    await act(async () => { await store.send() })

    expect(store.get().busy).toBe(false)
    expect(store.get().makeErr).toBe('gui.persona.make_failed')
  })
})

describe('the wall of saved personas', () => {
  it('lists only artifacts that carry a coordinator seat', async () => {
    install([
      row('travel-concierge'),
      row('nightly-report', { artifact_kind: 'workflow', coordinator: false }),
      row('worker-table', { coordinator: false }),
      row('composite-persona', { artifact_kind: 'composite' }),
    ])
    render(<PersonaApp />)
    await act(async () => { store.show('saved'); await store.load() })

    expect(await screen.findByText('travel-concierge')).toBeTruthy()
    expect(screen.getByText('composite-persona')).toBeTruthy()
    expect(screen.queryByText('nightly-report')).toBeNull()
    expect(screen.queryByText('worker-table')).toBeNull()
  })

  it('names the sub-agent behind every seat, not just the local label', async () => {
    install([row('travel-concierge', {
      workers: [
        { label: 'route-researcher', agent: 'Raven-Research' },
        { label: 'itinerary-formatter', agent: 'Raven-Design' },
      ],
    })])
    render(<PersonaApp />)
    await act(async () => { store.show('saved'); await store.load() })

    /* "Which sub-agents did it pick?" was answerable only by hovering, because
       the agent lived in a title attribute and the card drew the label alone. */
    /* One identity plus two delegates, each naming where it comes from. */
    expect(await screen.findByText('gui.persona.seat_main')).toBeTruthy()
    expect(screen.getByText('gui.persona.host')).toBeTruthy()
    expect(screen.getByText('travel-concierge coordinator brief')).toBeTruthy()
    expect(screen.getByText('route-researcher')).toBeTruthy()
    expect(screen.getByText('Raven-Research')).toBeTruthy()
    expect(screen.getByText('itinerary-formatter')).toBeTruthy()
    expect(screen.getByText('Raven-Design')).toBeTruthy()
  })

  it('stages the persona on the draft and leaves the page', async () => {
    withDraftButton()
    install([row('travel-concierge')])
    render(<PersonaApp />)
    await act(async () => { store.show('saved'); await store.load() })

    fireEvent.click(await screen.findByText('gui.persona.start'))

    expect(staged()).toBe('travel-concierge')
    expect(showPage).toHaveBeenCalledWith(null)
  })

  it('stays put, and stages nothing, when there is no draft to start', async () => {
    install([row('travel-concierge')])
    render(<PersonaApp />)
    await act(async () => { store.show('saved'); await store.load() })

    fireEvent.click(await screen.findByText('gui.persona.start'))

    expect(staged()).toBeNull()
    expect(showPage).not.toHaveBeenCalled()
  })

  it('asks before deleting, with the name in the question', async () => {
    const stub = install([row('travel-concierge')])
    render(<PersonaApp />)
    await act(async () => { store.show('saved'); await store.load() })

    fireEvent.click(await screen.findByText('gui.persona.delete'))

    /* One click arms it and nothing has gone yet: the wall is a list of things
       with names, and the second click happens under the right one. */
    expect(stub.removed).toEqual([])
    expect(screen.getByText('gui.persona.delete_sure')).toBeTruthy()

    await act(async () => { fireEvent.click(screen.getByText('gui.persona.delete_yes')) })
    expect(stub.removed).toEqual(['travel-concierge'])
  })

  it('keeps the wall on screen when a delete does not land', async () => {
    install([row('travel-concierge')])
    sources.persona = { ...sources.persona!, remove: async () => { throw new Error('gone') } }
    render(<PersonaApp />)
    await act(async () => { store.show('saved'); await store.load() })

    fireEvent.click(await screen.findByText('gui.persona.delete'))
    await act(async () => { fireEvent.click(screen.getByText('gui.persona.delete_yes')) })

    /* `err` is "the wall could not be read", and the wall draws it INSTEAD of
       the cards -- so a failed delete reported there takes every Persona off
       the screen. */
    expect(store.get().err).toBe('')
    expect(screen.getByText('travel-concierge')).toBeTruthy()
  })

  it('keeps it when the question is answered the other way', async () => {
    const stub = install([row('travel-concierge')])
    render(<PersonaApp />)
    await act(async () => { store.show('saved'); await store.load() })

    fireEvent.click(await screen.findByText('gui.persona.delete'))
    fireEvent.click(screen.getByText('gui.persona.delete_no'))

    expect(stub.removed).toEqual([])
    expect(store.get().confirming).toBe('')
    expect(screen.getByText('gui.persona.start')).toBeTruthy()
  })

  it('tells an unread wall apart from an empty one', async () => {
    install([])
    render(<PersonaApp />)
    act(() => { store.show('saved') })
    expect(screen.getByText('gui.persona.loading')).toBeTruthy()
    await act(async () => { await store.load() })
    expect(await screen.findByText('gui.persona.empty')).toBeTruthy()
  })
})
