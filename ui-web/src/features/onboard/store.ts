/* The first-run wizard's state and verbs.
 *
 * Page state, outside React on purpose: the boot opens the wizard
 * (app/boot.ts) and the page's wiring hands it the step bodies
 * (app/install.ts), neither of which is a component. The wizard owns only the
 * frame -- which step is up, what was skipped, the import the last step
 * starts -- and asks each step's owning domain, through the step body it was
 * handed, whether the step is loaded and done. */

import { pick as pickLang } from '../../state/lang/pick'
import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'
import { setupState } from '../model/source'

import type { Lang } from '../../state/lang'
import type { FoundAgent, ImportPlatform, ImportScan, OnboardSource, StepId, StepBodies } from './types'

export const STEPS: readonly StepId[] = ['model', 'search', 'agents', 'sync']

/* The agents and data-sync steps are held back from the strip for now: a first
   run asks only for a model and a search tool, then enters. Their bodies and
   verbs stay, so bringing them back is widening this list. */
const SHOWN: readonly StepId[] = ['model', 'search']
let shown: readonly StepId[] = SHOWN

/* The step body whose data each step draws on; the sync step waits on the
   embedding model's. */
const BODY_OF: Record<StepId, keyof StepBodies> = { model: 'model', search: 'search', agents: 'agents', sync: 'memory' }

/** How long the closing fade runs before the island comes down (styles.css). */
export const CLOSE_MS = 500

export interface OnboardState {
  open: boolean
  /* Bumped on every open, so a reopened wizard starts from a fresh tree. */
  epoch: number
  step: StepId
  skipped: Partial<Record<StepId, true>>
  bodies: StepBodies | null
  /* null until the importer has answered; the sync step is offered only once
     it has, and only when it can run. */
  scan: ImportScan | null
  syncPick: Record<string, boolean>
  busy: boolean
  closing: boolean
  error: string
}

const initial = (): OnboardState => ({
  open: false,
  epoch: 0,
  step: 'model',
  skipped: {},
  bodies: null,
  scan: null,
  syncPick: {},
  busy: false,
  closing: false,
  error: '',
})

const store = makeStore<OnboardState>(initial())

export const { get, subscribe } = store

export function set(patch: Partial<OnboardState>): void {
  store.set((prev) => ({ ...prev, ...patch }))
}

const source = (): OnboardSource => ds('onboard')

/* The bodies are the page's to hand over, once, before the boot can open the
   wizard; kept across opens because the domains behind them are singletons. */
export function setBodies(bodies: StepBodies): void {
  set({ bodies })
}

let scanning: Promise<void> | null = null
let closeTimer: ReturnType<typeof setTimeout> | null = null

/* Every shown step's data is asked for at once: the scan and the probe are
   slow and the reader spends the first step on the provider form, so by the
   time they reach the agents step the answers are in. A hidden step's data is
   not asked for at all -- the roster's load probes every agent. */
export function open(): void {
  const bodies = get().bodies
  if (!bodies) return
  if (closeTimer) { clearTimeout(closeTimer); closeTimer = null }
  set({ ...initial(), bodies, open: true, epoch: get().epoch + 1 })
  for (const key of new Set(shown.map((id) => BODY_OF[id]))) void bodies[key].load().catch(() => {})
  if (shown.includes('sync')) void scan()
}

/* One importer read. The latest one issued is the only one whose answer
   lands: the read from opening and the re-read on leaving the agents step go
   to the same call, and a slower earlier read arriving after a later one would
   otherwise put the reader on a step the strip no longer shows. */
function scan(): Promise<void> {
  const p: Promise<void> = source()
    .scan()
    .then((res) => { if (scanning === p) set({ scan: res }) })
    .catch((e: unknown) => { if (scanning === p) set({ scan: { ready: false, reason: failure(e), platforms: [] } }) })
    .finally(() => { if (scanning === p) scanning = null })
  scanning = p
  return p
}

export function isOpen(): boolean {
  return get().open
}

const failure = (e: unknown): string => {
  const err = e as { data?: { detail?: string }; message?: string } | null
  return (err && ((err.data && err.data.detail) || err.message)) || String(e)
}

export const found = (): FoundAgent[] => get().bodies?.agents.found() ?? []

/* The importer's row for one found agent, when it knows the platform at all. */
export const platformOf = (agent: FoundAgent): ImportPlatform | undefined =>
  get().scan?.platforms.find((p) => p.platform === agent.id)

/* Whether there is anything to import, and whether an import can run yet,
   are the sync step's to say. The step itself is always on the strip: one
   that came and went with the memory model or the agents found left the
   reader no way to learn what it waited on. */
export const syncable = (): FoundAgent[] => found().filter((a) => platformOf(a)?.scannable)

/* An import waits on an embedding model, picked on this very step when step
   one did not set one. */
export const syncReady = (): boolean => !!get().bodies?.memory.done()

export function visibleSteps(): StepId[] {
  return [...shown]
}

export function stepDone(id: StepId): boolean {
  const s = get()
  if (!s.bodies) return false
  if (id === 'model') return s.bodies.model.done()
  /* Search is optional: nothing on the step holds the reader. */
  if (id === 'search') return true
  if (id === 'agents') return s.bodies.agents.done()
  /* Nothing to import is a finished step: the wizard ends on it. */
  if (!syncable().length) return true
  return syncReady() && Object.values(s.syncPick).some(Boolean)
}

/* Which steps carry a Skip. The model step has nothing to skip -- without a
   chat model nothing runs -- and the search step is always done, so a Skip
   there would only be a second way to press the primary. */
export const skippable = (id: StepId): boolean => id !== 'model' && id !== 'search'

export function isLast(id: StepId): boolean {
  const steps = visibleSteps()
  return steps[steps.length - 1] === id
}

const move = (delta: number): void => {
  const steps = visibleSteps()
  const at = steps.indexOf(get().step)
  const to = Math.max(0, Math.min(steps.length - 1, at + delta))
  set({ step: steps[to] as StepId, error: '' })
}

/* Leaving the agents step forward asks the importer again before deciding
   what comes after it: whether an import can run turns on the memory model,
   which the first step may have saved after the answer from opening came in.
   A wizard that read it once decided the last step before the reader had done
   the one thing that makes it appear. Every forward verb goes through here --
   next, skip and the footer's finish, which the page picks on the stale answer
   and which is therefore not allowed to close on it. */
async function settleAfterAgents(): Promise<void> {
  if (get().step !== 'agents') return
  set({ busy: true })
  try { await scan() } finally { set({ busy: false }) }
}

export async function next(): Promise<void> {
  await settleAfterAgents()
  move(1)
}

export function back(): void {
  move(-1)
}

export async function skip(): Promise<void> {
  const id = get().step
  set({ skipped: { ...get().skipped, [id]: true } })
  await settleAfterAgents()
  if (isLast(id)) await close()
  else move(1)
}

export function toggleSync(platform: string): void {
  const pick = { ...get().syncPick }
  pick[platform] = !pick[platform]
  set({ syncPick: pick })
}

/* The last step's primary: starts the import from the sync step, otherwise
   just hands the window to the chat page behind. On the agents step the page
   chose this verb on the answer it had; the fresh one may add a step, and then
   this is a move rather than an exit. */
export async function finish(): Promise<void> {
  await settleAfterAgents()
  if (!isLast(get().step)) { move(1); return }
  if (get().step === 'sync' && syncable().length) {
    const platforms = Object.entries(get().syncPick).filter(([, on]) => on).map(([id]) => id)
    set({ busy: true, error: '' })
    try {
      // Memory files only from the web: conversations take hours and stay a CLI option.
      const r = await source().startImport(platforms, 'memory_files')
      if (!r.started) { set({ error: r.detail || 'import did not start' }); return }
    } catch (e) {
      set({ error: failure(e) })
      return
    } finally {
      set({ busy: false })
    }
  }
  await close()
}

/* The page's first-run redirects read `setupState`; it is re-read here so a
   reader who just connected a provider is not sent to Settings on their first
   Send. Then the fade, then the island comes down. */
export async function close(): Promise<void> {
  if (!get().open || get().closing) return
  set({ closing: true })
  setupState.providerConfigured = await source().providerConfigured()
  closeTimer = setTimeout(() => {
    closeTimer = null
    set({ open: false, closing: false })
  }, CLOSE_MS)
}

export function setLang(v: Lang): void {
  void pickLang(v, { persist: true })
}

/* The held-back steps' tests put them back on the strip. */
export function _showStepsForTests(steps: readonly StepId[]): void {
  shown = steps
}

export function _resetForTests(): void {
  if (closeTimer) { clearTimeout(closeTimer); closeTimer = null }
  scanning = null
  shown = SHOWN
  store._resetForTests()
  store.set(initial())
}
