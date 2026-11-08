/* Two beats, one for each question the view keeps asking the gateway.
 *
 * The STATE chain asks `trajectory.state` every five seconds for as long as
 * the gateway announces the surface, whichever view is up: a view switched
 * off under the reader goes away, and one switched on again gets its toggle
 * back without a reconnect. It stops only when the surface is refused
 * outright; the next handshake (src/app/install.ts's reconnect path) forgets
 * the refusal and brings it back.
 *
 * The DATA chain runs while the trajectory is on screen for a conversation
 * the toggle may show, and every beat does one of two things. While the
 * store holds no whole snapshot it reads one (`store.load`), and reads again
 * on its slow beat if that failed; once a snapshot is committed it asks
 * `trajectory.changes` from the watermark -- half a second apart, two seconds
 * after ten quiet seconds, straight back to half a second on the first
 * change, again at once on `has_more`. A reset from the feed marks the
 * snapshot stale and the next beat is a read again. So the first read, a
 * re-read and the feed are one serial chain with one request in the air, and
 * no feed request is ever made against a snapshot that is not whole.
 *
 * A hidden tab pauses both chains; the tab coming back to the front (`sync`,
 * spent by state/visibility.ts's slot) resumes them at once. Both chains
 * watch the store rather than being told: `install` subscribes once, and
 * every write re-evaluates whether each chain is wanted. An answer from an
 * earlier generation is dropped and the chain takes a fresh beat from the
 * state as it now stands, so nothing stale ever sets the next interval.
 */

import { has, servesTrajectory } from '../../rpc/capabilities'
import * as store from './store'

import type { TrajectoryChangesResult } from './types'

export const FAST_MS = 500
export const SLOW_MS = 2000
/** Quiet beats at the fast rate before slowing: ten seconds' worth. */
export const QUIET_BEATS = 20
export const STATE_EVERY_MS = 5000

const visible = (): boolean => typeof document === 'undefined' || document.visibilityState !== 'hidden'

/* One chain: whether it is armed, its pending timer and the request in the
   air. `beat` is the chain's own; the rest is shared bookkeeping. The request
   is a token rather than a flag: a chain stopped and started again while an
   answer is still out must be free to ask afresh, and that old answer, when
   it lands, must not clear the newer request's place. */
class Chain {
  running = false
  timer: ReturnType<typeof setTimeout> | null = null
  private inflight: number | null = null
  private seq = 0

  constructor(private readonly beat: () => Promise<void>) {}

  busy(): boolean {
    return this.inflight !== null
  }

  begin(): number {
    this.seq += 1
    this.inflight = this.seq
    return this.seq
  }

  end(token: number): void {
    if (this.inflight === token) this.inflight = null
  }

  clear(): void {
    if (this.timer !== null) clearTimeout(this.timer)
    this.timer = null
  }

  schedule(ms: number): void {
    this.clear()
    if (!this.running) return
    this.timer = setTimeout(() => { this.timer = null; void this.beat() }, ms)
  }

  start(): void {
    if (this.running) return
    this.running = true
    void this.beat()
  }

  stop(): void {
    this.running = false
    this.clear()
    this.inflight = null
  }

  /* After a dropped answer or a visibility change: a fresh beat from the
     current state, unless one is already in the air or already due. */
  kick(): void {
    if (this.running && this.inflight === null && this.timer === null) void this.beat()
  }
}

let quiet = 0
let unsubscribe: (() => void) | null = null

/* ── the state chain ──────────────────────────────────────────────────── */

const stateWanted = (): boolean => servesTrajectory() && has('trajectory')

const stateChain = new Chain(async () => {
  if (!stateChain.running || stateChain.busy()) return
  if (!stateWanted()) { stateChain.stop(); return }
  if (!visible()) return
  const token = stateChain.begin()
  try {
    await store.refreshState()
  } finally {
    stateChain.end(token)
  }
  if (!stateChain.running) return
  if (!stateWanted()) { stateChain.stop(); return }
  stateChain.schedule(STATE_EVERY_MS)
})

/* ── the data chain ───────────────────────────────────────────────────── */

const dataWanted = (s: store.TrajectoryState = store.get()): boolean =>
  s.view === 'trajectory' && store.available(s)

const nextBeat = (): number => (quiet >= QUIET_BEATS ? SLOW_MS : FAST_MS)

/* `changes` answered under the generation it was asked in. A batch that moved
   anything resets the quiet count; `has_more` asks again at once; a reset
   marks the snapshot stale, and the next beat reads it over. */
function applied(batch: TrajectoryChangesResult): void {
  if (batch.reset_required) {
    store.snapshotStale()
    dataChain.schedule(0)
    return
  }
  store.applyChanges(batch)
  const moved = batch.upserts.length > 0 || batch.removed.length > 0
  quiet = moved ? 0 : quiet + 1
  dataChain.schedule(batch.has_more ? 0 : nextBeat())
}

const dataChain = new Chain(async () => {
  if (!dataChain.running || dataChain.busy()) return
  const s = store.get()
  if (!dataWanted(s)) { dataChain.stop(); return }
  if (!visible()) return
  const src = store.source()
  if (!src) { dataChain.stop(); return }
  const g = store.gen()
  const token = dataChain.begin()
  if (!s.snapshotReady) {
    /* The walk moves the generation itself and checks it page by page; what
       is left to decide here is only when to come back. */
    try {
      await store.load()
    } finally {
      dataChain.end(token)
    }
    /* Stopped meanwhile, or started again with a request of its own in the
       air: this beat has nothing more to decide. */
    if (!dataChain.running || dataChain.busy()) return
    const after = store.get()
    if (!dataWanted(after)) { dataChain.stop(); return }
    quiet = 0
    dataChain.schedule(after.snapshotReady ? 0 : SLOW_MS)
    return
  }
  try {
    const batch = await src.changes(s.sessionKey as string, s.epoch as string, s.revision)
    dataChain.end(token)
    if (!dataChain.running || dataChain.busy()) return
    if (g !== store.gen()) { dataChain.kick(); return }
    applied(batch)
  } catch (e) {
    dataChain.end(token)
    if (!dataChain.running || dataChain.busy()) return
    if (g !== store.gen()) { dataChain.kick(); return }
    store.liveFailed(e)
    if (dataWanted()) dataChain.schedule(nextBeat())
    else dataChain.stop()
  }
})

/* ── wiring ───────────────────────────────────────────────────────────── */

/* The store's own writes decide whether each chain runs; `sync` asks the same
   question from outside, for the signals that are not store writes -- a
   handshake, the tab coming back to the front. */
function follow(): void {
  if (stateWanted()) stateChain.start()
  else stateChain.stop()
  if (dataWanted()) dataChain.start()
  else dataChain.stop()
}

/** Re-evaluate both chains and, when the tab is in front, beat at once. */
export function sync(): void {
  follow()
  if (!visible()) return
  stateChain.clear()
  stateChain.kick()
  dataChain.clear()
  dataChain.kick()
}

/** Wire the chains to the store, once. */
export function install(): void {
  if (unsubscribe) return
  unsubscribe = store.subscribe(follow)
  follow()
}

export const isStateRunning = (): boolean => stateChain.running
export const isDataRunning = (): boolean => dataChain.running

export function _resetForTests(): void {
  stateChain.stop()
  dataChain.stop()
  if (unsubscribe) { unsubscribe(); unsubscribe = null }
  quiet = 0
}
