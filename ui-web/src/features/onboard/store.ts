/* The page-scoped opening state for the first-run onboarding island. */

import { ds } from '../../state/sources'
import { makeStore } from '../../state/store'

import type { OnboardSource } from './types'

export interface OnboardOpening {
  source: OnboardSource | null
  epoch: number
}

const store = makeStore<OnboardOpening>({ source: null, epoch: 0 })

export const { get, set, subscribe } = store

let resolveOpen: (() => void) | null = null

export function open(): Promise<void> {
  resolveOpen?.()
  return new Promise(resolve => {
    resolveOpen = resolve
    set({ source: ds('onboard'), epoch: get().epoch + 1 })
  })
}

export function finish(): void {
  if (!get().source) return
  const done = resolveOpen
  resolveOpen = null
  set({ source: null, epoch: get().epoch })
  done?.()
}

export function _resetForTests(): void {
  resolveOpen?.()
  resolveOpen = null
  set({ source: null, epoch: 0 })
}
