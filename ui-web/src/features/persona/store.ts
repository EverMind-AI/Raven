/* Page state for the persona page: the maker, and the wall of saved ones.
 *
 * Two views, because they are two jobs. The page opens on the maker -- a
 * conversation whose only subject is the Persona being described -- and the
 * wall behind it is what was kept. Making one and talking to one are different
 * enough that a reader on the wall is not looking for a composer, and a reader
 * describing one is not browsing.
 *
 * The maker is a CONVERSATION, not a form. Designing a Persona takes more than
 * one sentence: the engine asks back ("there is already one by that name --
 * replace it?"), and the reader says "fewer workers" and means the one just
 * described. It was a single send before, and both of those left the page
 * waiting for a draft that could not arrive -- the question had gone to a
 * composer sheet the reader was not looking at, and the refinement had nowhere
 * to go. So the page keeps one conversation, claims its questions
 * (state/clarifyClaim.ts), and draws what was said.
 *
 * What the turn leaves behind is a DRAFT (raven/agent/loop/wiring.py): nothing
 * is written until `save` below, which is what stops a library filling with a
 * row per turn. And the conversation is the maker's own -- a reader who wants
 * to TALK to a Persona starts that conversation from the wall, which is what
 * `start` does.
 *
 * The draft and the transcript are polled rather than pushed. `turn.send`
 * answers when the turn is accepted, not when it has run, and this page has no
 * transcript island to subscribe through -- so it asks, at a human interval,
 * until the turn has left something to draw.
 */

import { t } from '../../i18n/t'
import { claim, release } from '../../state/clarifyClaim'
import { stage as stageHarness } from '../../state/harness'
import { show as showPage } from '../../state/page'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { show as toast } from '../../state/toast'

import type { ClarifyAsk } from '../../state/clarifyClaim'
import type { PersonaLine, PersonaRow, PersonaStep } from './types'

export type PersonaView = 'make' | 'saved'

/** A question the engine asked about the Persona being described. */
export interface PendingAsk {
  requestId: string
  question: string
  choices: readonly string[]
}

export interface PersonaState {
  /** Which of the page's two jobs is on screen. */
  view: PersonaView
  /* null = not read yet, which is not the same as "no personas": one draws a
     skeleton, the other says the wall is empty. */
  rows: PersonaRow[] | null
  err: string
  query: string
  /** The conversation the describing happens in, minted on the first send. */
  session: string
  /** What has been said in it. */
  lines: PersonaLine[]
  /** What the reader has typed but not sent. */
  text: string
  /** A turn is out and has not come back with anything to draw yet. */
  busy: boolean
  /** Seconds spent on the turn in flight, so a long wait reads as progress. */
  waited: number
  /** What the turn is doing right now, straight off its own frames. */
  doing: PersonaStep | null
  /** The tools it has run, in order, so the wait shows its work. */
  ran: string[]
  /** The engine is blocked on an answer from the reader. */
  ask: PendingAsk | null
  /** The generated Persona, before anyone decided to keep it. */
  draft: PersonaRow | null
  /** The name it will be saved under, which the reader may change. */
  name: string
  /** The saved Persona a delete is waiting to be confirmed on. */
  confirming: string
  saving: boolean
  makeErr: string
}

const EMPTY: PersonaState = {
  view: 'make', rows: null, err: '', query: '',
  session: '', lines: [], text: '', busy: false, waited: 0, doing: null, ran: [],
  ask: null,
  draft: null, name: '', confirming: '', saving: false, makeErr: '',
}

const store = makeStore<PersonaState>({ ...EMPTY })

export const { get, set, subscribe } = store

const patch = (next: Partial<PersonaState>): void => store.set({ ...store.get(), ...next })

const source = () => ds('persona')

/* How long the page waits for a turn to leave something, and how often it asks.
   Ten minutes, because generation is a model call behind a tool call and a long
   brief is a long call: a detailed travel persona with four workers measured
   113s, and the ninety seconds this used to allow gave up twenty-three seconds
   before its own draft landed. The reader is told how long they have been
   waiting rather than left guessing, and a wait that really does outlive this
   can be picked up with `recheck` rather than started again. */
export const POLL_MS = 2000
export const POLL_TRIES = 300

/* The answer callback for the question on screen. Held outside the store
   because it is a function the transport handed us, not state to render. */
let respond: ((text: string) => void) | null = null

/* How to stop following the maker's turn, held for the same reason. */
let unwatch: (() => void) | null = null

/* A Persona is the coordinator seat, not the worker table: a stored Harness
   without one configures workers for a turn and has no identity to run as, so
   it belongs on the playbook library and not here. */
export const isPersona = (r: PersonaRow): boolean =>
  r.coordinator && (r.artifact_kind === 'harness' || r.artifact_kind === 'composite')

export async function load(): Promise<void> {
  try {
    const rows = await source().list()
    patch({ rows: rows.filter(isPersona), err: '' })
  } catch (e) {
    patch({ rows: [], err: (e as Error)?.message || String(e) })
  }
}

export function visible(): PersonaRow[] {
  const state = store.get()
  const q = state.query.trim().toLowerCase()
  const rows = state.rows || []
  if (!q) return rows
  return rows.filter(r => `${r.name} ${r.description}`.toLowerCase().includes(q))
}

export function search(query: string): void {
  patch({ query })
}

export function show(view: PersonaView): void {
  patch({ view })
  if (view === 'saved') void load()
}

export function type(text: string): void {
  patch({ text })
}

const wait = (ms: number): Promise<void> => new Promise((done) => { setTimeout(done, ms) })

/** Take the questions asked about the maker's own conversation. */
function claimQuestions(session: string): void {
  claim(session, {
    ask: (asked: ClarifyAsk, answer: (text: string) => void) => {
      respond = answer
      /* The turn is blocked on the reader now, so the page stops waiting and
         starts asking -- a spinner over a question nobody can see is the bug
         this whole seam exists to fix. */
      patch({ busy: false, ask: { requestId: asked.requestId, question: asked.question, choices: asked.choices } })
    },
    close: (requestId: string) => {
      if (store.get().ask?.requestId !== requestId) return
      respond = null
      patch({ ask: null })
    },
  })
}

/* Asked once before any wait, so a turn that already left something costs no
   delay -- and so a test does not have to run the clock to see the happy path.
   Stops on whichever the turn produced first: a draft, a question, or a reply. */
async function watch(session: string, saidBefore: number): Promise<void> {
  const began = Date.now()
  for (let tries = 0; tries < POLL_TRIES; tries++) {
    const [draft, lines] = await Promise.all([source().draft(session), source().history(session)])
    const replied = lines.filter((l) => l.role === 'assistant').length > saidBefore
    patch({ waited: Math.round((Date.now() - began) / 1000), ...(lines.length ? { lines } : {}) })
    if (draft) {
      patch({ busy: false, draft, name: draft.name })
      return
    }
    if (store.get().ask) return
    if (replied) {
      patch({ busy: false })
      return
    }
    await wait(POLL_MS)
  }
  patch({ busy: false, makeErr: t('gui.persona.slow') })
}

/** Ask once more for a draft this page stopped waiting on. */
export async function recheck(): Promise<void> {
  const { session, busy } = store.get()
  if (!session || busy) return
  patch({ busy: true, makeErr: '' })
  const draft = await source().draft(session).catch(() => null)
  const lines = await source().history(session).catch(() => [])
  patch({ busy: false, ...(lines.length ? { lines } : {}) })
  if (draft) patch({ draft, name: draft.name })
  else patch({ makeErr: t('gui.persona.slow') })
}

/** Say the next thing in the making conversation. */
export async function send(): Promise<void> {
  const state = store.get()
  const text = state.text.trim()
  if (!text || state.busy) return
  /* Drawn before the round trip: the reader's own line is not news from the
     server, and a composer that empties into silence reads as a dropped
     message. */
  patch({ busy: true, waited: 0, doing: null, ran: [], makeErr: '', text: '', lines: [...state.lines, { role: 'user', text }] })
  try {
    let session = state.session
    if (!session) {
      session = await source().openMaker()
      claimQuestions(session)
      patch({ session })
      /* Following the turn is what makes this page something other than a
         spinner: the frames say what it is doing, and a reader who can see
         "running create_persona_playbook" is not waiting on a black box. */
      unwatch = await source().watch(session, (step) => {
        const seen = store.get().ran
        patch({
          doing: step,
          ran: step.kind === 'tool' && step.label && seen[seen.length - 1] !== step.label
            ? [...seen, step.label]
            : seen,
        })
      }).catch(() => null)
    }
    const saidBefore = store.get().lines.filter((l) => l.role === 'assistant').length
    await source().describe(session, text)
    await watch(session, saidBefore)
  } catch (e) {
    patch({ busy: false, makeErr: t('gui.persona.make_failed', { err: (e as Error)?.message || String(e) }) })
  }
}

/** Answer the question the engine is blocked on, and wait for the turn again. */
export async function answer(text: string): Promise<void> {
  const state = store.get()
  const reply = text.trim()
  if (!state.ask || !reply || !respond) return
  const send = respond
  respond = null
  patch({ ask: null, busy: true, waited: 0, doing: null, ran: [], text: '', lines: [...state.lines, { role: 'user', text: reply }] })
  send(reply)
  const saidBefore = store.get().lines.filter((l) => l.role === 'assistant').length
  await watch(state.session, saidBefore)
}

export function rename(name: string): void {
  patch({ name })
}

/** Keep the draft. The wall is what it lands on, so that is where the page goes. */
export async function save(): Promise<void> {
  const state = store.get()
  if (!state.draft || state.saving) return
  patch({ saving: true })
  try {
    const saved = await source().save(state.session, state.name.trim() || undefined)
    patch({ saving: false, draft: null, name: '', view: 'saved' })
    toast(t('gui.persona.saved_one', { name: saved }))
    await load()
  } catch (e) {
    patch({ saving: false, makeErr: t('gui.persona.make_failed', { err: (e as Error)?.message || String(e) }) })
  }
}

/** Let the draft go. The conversation stays, so the reader can keep describing. */
export async function discard(): Promise<void> {
  const state = store.get()
  if (!state.draft) return
  patch({ draft: null, name: '' })
  try {
    await source().discard(state.session)
  } catch {
    /* The draft is gone from the page either way, and the engine drops it with
       the session. Nothing a reader could do about a failure here. */
  }
}

/** Put the maker back to an empty conversation, leaving the library alone. */
export function reset(): void {
  const { session, view, rows, query } = store.get()
  if (session) release(session)
  respond = null
  unwatch?.()
  unwatch = null
  store.set({ ...EMPTY, view, rows, query })
}

/* Deleting is a two-step on the card rather than a dialog over the page: the
   wall is a short list of things with names, and a reader who clicked the wrong
   row sees whose name is in the question before the second click. */
export function askRemove(name: string): void {
  patch({ confirming: name })
}

export function cancelRemove(): void {
  patch({ confirming: '' })
}

/** Take a saved Persona out of the library. A conversation already running on
    it keeps running: the binding is a snapshot, not a lookup. */
export async function remove(name: string): Promise<void> {
  patch({ confirming: '' })
  try {
    await source().remove(name)
    toast(t('gui.persona.deleted', { name }))
    await load()
  } catch (e) {
    /* Toasted rather than put on `err`: that field is "the wall could not be
       read", and the wall draws it INSTEAD of the cards -- so reporting a
       failed delete there would take every saved Persona off the screen over
       one call that did not land. */
    toast(t('gui.persona.delete_failed', { err: (e as Error)?.message || String(e) }))
  }
}

/* Opens a conversation that will be created on this Persona: the pick is
   staged, a fresh draft is opened the way every other "start a task with
   something named" does (features/composer/startTaskWith.ts), and the page
   closes so the reader lands in the conversation they just started rather than
   on the wall they started it from. */
export function start(name: string): void {
  const newBtn = document.getElementById('newBtn')
  if (!newBtn) {
    toast(t('gui.persona.start_failed', { name }))
    return
  }
  stageHarness(name)
  newBtn.click()
  showPage(null)
}

export function openPage(): void {
  /* The conversation outlives the page, so its questions have to be claimed
     again on the way back in -- `closePage` hands them over rather than
     holding on to them while nobody is looking. */
  const { session } = store.get()
  if (session) claimQuestions(session)
  showPage('personaPage')
  void load()
}

export function closePage(): void {
  /* The questions go back to the sheet. A claim held by a page the reader has
     left is a question asked where nobody can see it: the sheet never opens,
     and the turn waits on an answer that cannot be given. */
  const { session } = store.get()
  if (session) release(session)
  showPage(null)
}

/* Every visible string comes from t(), so a re-render is the whole redraw. */
export function redraw(): void {
  patch({})
}

export function _resetForTests(): void {
  const { session } = store.get()
  if (session) release(session)
  respond = null
  unwatch?.()
  unwatch = null
  store.set({ ...EMPTY })
}
