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
import type { FoundAgent, ImportPlatform, ImportScan, ImportTier, OnboardSource, StepId, StepBodies } from './types'

export const STEPS: readonly StepId[] = ['model', 'search', 'agents', 'sync']

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
  /* Memory files by default: they are the preferences and project knowledge,
     and they land in minutes. Conversations are hours of background work, so
     they are the reader's choice, not the wizard's. */
  syncTier: ImportTier
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
  syncTier: 'memory_files',
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

/* Every step's data is asked for at once: the scan and the probe are slow and
   the reader spends the first step on the provider form, so by the time they
   reach the agents step the answers are in. */
export function open(): void {
  const bodies = get().bodies
  if (!bodies) return
  if (closeTimer) { clearTimeout(closeTimer); closeTimer = null }
  set({ ...initial(), bodies, open: true, epoch: get().epoch + 1 })
  for (const body of Object.values(bodies)) void body.load().catch(() => {})
  void scan()
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

/* The sync step exists only where there is something to sync: an import that
   can run, and an agent the importer can read. */
export function syncVisible(): boolean {
  const scan = get().scan
  return !!scan && scan.ready && found().some((a) => platformOf(a)?.scannable)
}

export function visibleSteps(): StepId[] {
  return STEPS.filter((id) => id !== 'sync' || syncVisible())
}

export function stepDone(id: StepId): boolean {
  const s = get()
  if (!s.bodies) return false
  if (id === 'model') return s.bodies.model.done()
  if (id === 'search') return s.bodies.search.done()
  if (id === 'agents') return s.bodies.agents.done()
  return Object.values(s.syncPick).some(Boolean)
}

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

export function setSyncTier(tier: ImportTier): void {
  set({ syncTier: tier })
}

/* What the picked agents hold, for the tier rows: the scan's counts summed over
   the switches that are on. */
export function pickedCounts(): { files: number; convs: number } {
  const s = get()
  let files = 0
  let convs = 0
  for (const agent of found()) {
    if (!s.syncPick[agent.id]) continue
    const p = platformOf(agent)
    if (!p) continue
    files += p.memory_files
    convs += p.conversations
  }
  return { files, convs }
}

/* The last step's primary: starts the import from the sync step, otherwise
   just hands the window to the chat page behind. On the agents step the page
   chose this verb on the answer it had; the fresh one may add a step, and then
   this is a move rather than an exit. */
export async function finish(): Promise<void> {
  await settleAfterAgents()
  if (!isLast(get().step)) { move(1); return }
  if (get().step === 'sync') {
    const platforms = Object.entries(get().syncPick).filter(([, on]) => on).map(([id]) => id)
    set({ busy: true, error: '' })
    try {
      const r = await source().startImport(platforms, get().syncTier)
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

export function _resetForTests(): void {
  if (closeTimer) { clearTimeout(closeTimer); closeTimer = null }
  scanning = null
  store._resetForTests()
  store.set(initial())
}
