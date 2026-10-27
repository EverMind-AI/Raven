/* The reasoning effort of this conversation: the row of the model picker that
 * offers it (features/model/ModelPicker.tsx, its EffortRow) and everything that
 * decides behind that row.
 *
 * Built the way state/tier.ts next door is, and for the same reasons: the row
 * renders from it -- `offered`, `options`, `heading`, `scopeNote` -- and a
 * click paints from the REPLY, never from the click, because the handler is
 * what decides whether the word was accepted.
 *
 * What it moves is raven's own thinking before each answer, and nothing else:
 * the tier beside it is what reaches sub-agents. The pick is the session's own
 * and the server keeps it on the session's record (raven.agent.session_effort),
 * so a resumed conversation comes back at what it was left at, gateway restart
 * or not -- which is the one way it differs from the tier.
 *
 * `session.set_effort` answers every call -- report, set and clear -- with the
 * pick, what the session runs at without one (`default`), and the words
 * (`options`), so one round trip is enough to draw the control. The first rung
 * is always the default: the pick cleared, the session following its mode's
 * effort or the configured `agents.defaults.reasoningEffort`. The words are
 * the model's: its provider's answer, four on most wires and six (xhigh and
 * max over them) on the Anthropic Messages wire, which is why the row is
 * re-read when the conversation's model is switched (features/model/source.ts).
 * A gateway older than this page refuses the method, the read fails, and the
 * row stays hidden rather than drawing a control over nothing.
 */

import { t } from '../i18n/t'
import { ds } from './sources'
import { makeStore } from './store'
import { show as toast } from './toast'

export interface EffortReply {
  session_key?: string
  /** The session's own pick, or null while it follows the default. */
  effort?: string | null
  /** What it runs at without a pick; null for the provider's own default. */
  default?: string | null
  /** The words `effort` accepts, least first. */
  options?: string[]
}

/* The seam, published by the live layer over `session.set_effort` and by the
   demo shell over a fixture. `set(null)` clears: the session follows the
   default again. */
export interface EffortSource {
  read(): Promise<EffortReply>
  set(effort: string | null): Promise<EffortReply>
}

/** One rung of the row. The default rung's id is '' -- no pick. */
export interface EffortRow {
  readonly id: string
  readonly name: string
  readonly ticked: boolean
}

export interface EffortState {
  /** Whether a reply has landed and offered words. */
  readonly loaded: boolean
  /** The pick, or '' while the session follows the default. */
  readonly effort: string
  /** What the session runs at without a pick, or '' for the provider's own. */
  readonly fallback: string
  readonly options: readonly string[]
}

const shut: EffortState = { loaded: false, effort: '', fallback: '', options: [] }
const store = makeStore<EffortState>(shut)

/** The store's state, for the row's useSyncExternalStore, and the page's
    store shape (state/store.ts): `set` is the store's own, with nothing to
    dedupe -- every write here comes from a reply. */
export const { get, set, subscribe, _resetForTests } = store

/* The words, in the reader's language. The server sends ids; what they are
   called is this side's, the way the tier's rungs are. A word this build does
   not know is shown as the server spelt it rather than hidden: the server is
   the one entitled to add a rung. */
const NAME: Record<string, string> = {
  minimal: 'gui.effort.minimal',
  low: 'gui.effort.low',
  medium: 'gui.effort.medium',
  high: 'gui.effort.high',
  xhigh: 'gui.effort.xhigh',
  max: 'gui.effort.max',
}

export function label(id: string): string {
  if (!id) return t('gui.effort.default')
  const key = NAME[id]
  return key ? t(key) : id
}

/** Whether there is a control to draw: a reply landed and it offered words. */
export const offered = (): boolean => get().loaded && get().options.length > 0

/** The pick in force, or '' for the default. */
export const current = (): string => get().effort

/** What "default" means right now, as a word, or '' for the provider's own. */
export const fallback = (): string => get().fallback

/** The default rung first, then every word the server offers, then a pick
    the server lists no word for. */
export const options = (): readonly EffortRow[] => {
  if (!offered()) return []
  const { effort, options: words } = get()
  /* A pick the words do not carry: made on another model's wire (the
     conversation switched models since, and the words are the model's) and
     still the record's -- the next turn runs at the highest rung this wire
     takes. Drawn after the words, ticked, so it is seen and can be moved off,
     rather than a row with no rung pressed. */
  const rungs = effort && !words.includes(effort) ? [...words, effort] : words
  return [
    { id: '', name: label(''), ticked: effort === '' },
    ...rungs.map((id) => ({ id, name: label(id), ticked: id === effort })),
  ]
}

export const heading = (): string => t('gui.effort.title')

/* One sentence on what the row moves, and one on what the default rung stands
   for right now -- which is a fact about this install, so the row says it
   rather than leaving "default" to mean anything. */
export const scopeNote = (): string => {
  const now = get().fallback ? label(get().fallback) : t('gui.effort.provider')
  return `${t('gui.effort.scope')} ${t('gui.effort.now', { name: now })}`
}

function absorb(reply: EffortReply | null | undefined): void {
  if (!reply) return
  const words = Array.isArray(reply.options) ? reply.options.filter((w) => typeof w === 'string' && w) : []
  store.set({
    loaded: true,
    effort: typeof reply.effort === 'string' ? reply.effort : '',
    fallback: typeof reply.default === 'string' ? reply.default : '',
    options: words,
  })
}

/* Asked on every conversation change, including the change to none: a draft
   shows the default its first turn will run at, and stages a pick until that
   turn mints a session (features/model/source.ts). A failure hides the row: a
   gateway without the method, or one that could not answer, leaves nothing
   honest to draw. */
export async function load(): Promise<void> {
  try {
    absorb(await ds('effort').read())
  } catch {
    store.set(shut)
  }
}

/* A rung's click. The one already in force asks for nothing. '' is the default
   rung, which clears the pick. */
export function pick(id: string): void {
  if (!get().loaded || id === get().effort) return
  void choose(id)
}

async function choose(id: string): Promise<void> {
  try {
    absorb(await ds('effort').set(id || null))
  } catch (err) {
    toast(t('gui.effort.failed', { name: label(id) }))
    /* Nothing moved: the state still holds what the server last said. */
    store.set({ ...get() })
    void err
  }
}
