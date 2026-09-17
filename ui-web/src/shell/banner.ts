/* The strip above the transcript (#bannerHost): at most one standing notice
 * about something the reader has to act on outside this conversation.
 *
 * Which notice stands is state here, and <Banner/> (src/chrome/Banner.tsx) is
 * the pair of shapes it picks between. The decision stays in `draw` rather than
 * moving into the render for two reasons: it is a priority rather than a
 * composition, and it consults the capability list -- a read that throws when
 * the seam is not installed, which the caller has to hear about and a render
 * must not make.
 *
 * Order is the whole design: a memory fault beats a missing capability,
 * because a backend that stopped storing has been handing back normal-looking
 * replies the whole time, while an unconfigured search has been visibly
 * refusing. Whichever wins draws alone.
 *
 * `draw` commits synchronously: it is the statement the caller's next line
 * reads the page after.
 */

import { flushSync } from 'react-dom'

import { ds } from '../state/sources'

export interface BannerSource {
  /* Whether the websearch capability is installed but not yet configured.
     A read rather than a fact this module keeps: the answer lives in the
     capability list, which the plugins layer owns and the live layer fills. */
  websearchNeeds(): boolean
}

/** Which notice is standing, or none at all. */
export type BannerKind = 'fault' | 'websearch' | null

export interface BannerState {
  readonly kind: BannerKind
  /** The fault's detail, for the notice that carries one. */
  readonly detail: string | null
}

const NOTHING: BannerState = { kind: null, detail: null }

/* A standing memory fault, or null. Set from the `memory.health` event: three
   consecutive failed writes mean the backend is not coming back on its own. */
let fault: string | null = null
let state: BannerState = NOTHING
const listeners = new Set<() => void>()

/** The notice standing right now. */
export function get(): BannerState {
  return state
}

/** For useSyncExternalStore: called whenever which notice stands changes. */
export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/* Stores AND draws. It used to only store, so that the redraw would go out
   through the published drawBanner name and pick up the live layer's override
   of it -- and that override cleared the host, so storing a fault put nothing
   on screen. The override is gone (live/120-settings.js says its no to the one
   notice it means, through the source), and with it the reason for a setter
   whose effect depends on the caller remembering a second call. */
export function setFault(detail: string | null): void {
  fault = detail
  draw()
}

export function draw(): void {
  /* The host is rendered by the page's root now, and asking for it is what the
     writer this replaces opened with: no host is nothing to decide, which is
     also what keeps a bench with no chat column -- and none of the seam a
     decision would need -- quiet. */
  if (!document.getElementById('bannerHost')) return
  if (fault) {
    commit({ kind: 'fault', detail: fault })
    return
  }
  commit(source().websearchNeeds() ? { kind: 'websearch', detail: null } : NOTHING)
}

/* The reader waving the suggestion away. Forgotten rather than remembered: the
   next draw offers it again, which is what happened when the notice was rebuilt
   by every draw. */
export function dismiss(): void {
  commit(NOTHING)
}

function commit(next: BannerState): void {
  if (next.kind === state.kind && next.detail === state.detail) return
  state = next
  flushSync(() => {
    for (const fn of [...listeners]) fn()
  })
}

const source = (): BannerSource => ds<BannerSource>('banner')
