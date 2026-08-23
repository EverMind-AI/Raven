/* The page-scoped opening state for the first-run onboarding island. */

import { ds } from '../../shell/bridge'

import type { OnboardSource } from './types'

export interface OnboardOpening {
  source: OnboardSource | null
  epoch: number
}

let state: OnboardOpening = { source: null, epoch: 0 }
let resolveOpen: (() => void) | null = null
const subs = new Set<() => void>()

export const snapshot = (): OnboardOpening => state

export function subscribe(fn: () => void): () => void {
  subs.add(fn)
  return () => subs.delete(fn)
}

const announce = (): void => subs.forEach(fn => fn())

export function open(): Promise<void> {
  resolveOpen?.()
  return new Promise(resolve => {
    resolveOpen = resolve
    state = { source: ds<OnboardSource>('onboard'), epoch: state.epoch + 1 }
    announce()
  })
}

export function finish(): void {
  if (!state.source) return
  state = { source: null, epoch: state.epoch }
  const done = resolveOpen
  resolveOpen = null
  announce()
  done?.()
}

export function _resetForTests(): void {
  resolveOpen?.()
  resolveOpen = null
  state = { source: null, epoch: 0 }
  announce()
}
