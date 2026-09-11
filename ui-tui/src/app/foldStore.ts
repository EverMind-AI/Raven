// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// What the reader has opened, kept outside the component that draws it.
//
// A fold is a decision the reader made; the row it sits on is a value the
// runtime replaces. Holding the fold in the row's own component state tied the
// two together, so a row that changed -- a step landing in a turn still running
// -- unmounted and took the reader's decision with it. Every few seconds, the
// tool call they had just opened closed itself.
//
// Keyed by the fold's own id rather than by the row: `call:<toolCallId>` and
// `seg:<firstToolCallId>` are the transport's ids, stable for the life of the
// call, which is exactly as long as the decision should last.

import { atom } from 'nanostores'

interface Scope {
  closed: readonly string[]
  open: readonly string[]
}

/** Folds the reader has decided about, per scope. A scope is one transcript view. */
export const $folds = atom<Record<string, Scope>>({})

const scopeOf = (scope: string): Scope => $folds.get()[scope] ?? { closed: [], open: [] }

/**
 * Whether a fold is open.
 *
 * `defaultOpen` decides the untouched case, so a stretch can open itself on what
 * it contains. Holding closed ids as well as open ones is what keeps that
 * default from overriding the reader: without it, "closed" and "never seen" are
 * one value, and a row rebuilt mid-turn reopens what they just shut.
 */
export const isFoldOpen = (scope: string, key: string, defaultOpen = false): boolean => {
  const { closed, open } = scopeOf(scope)

  return open.includes(key) ? true : closed.includes(key) ? false : defaultOpen
}

export const openFolds = (scope: string): readonly string[] => scopeOf(scope).open

export const toggleFold = (scope: string, key: string, defaultOpen = false): void => {
  const all = $folds.get()
  const { closed, open } = scopeOf(scope)
  const nowOpen = !isFoldOpen(scope, key, defaultOpen)

  $folds.set({
    ...all,
    [scope]: {
      closed: nowOpen ? closed.filter(k => k !== key) : [...closed.filter(k => k !== key), key],
      open: nowOpen ? [...open.filter(k => k !== key), key] : open.filter(k => k !== key)
    }
  })
}

/** One scope's fold decisions, split by the key kind that carries them. */
export interface CallFolds {
  callClosed: ReadonlySet<string>
  callOpen: ReadonlySet<string>
  full: ReadonlySet<string>
  segClosed: ReadonlySet<string>
  segOpen: ReadonlySet<string>
}

export const EMPTY_CALL_FOLDS: CallFolds = {
  callClosed: new Set(),
  callOpen: new Set(),
  full: new Set(),
  segClosed: new Set(),
  segOpen: new Set()
}

/**
 * One scope's folds, for the height estimator, which draws no rows of its own
 * and so has to be told which transcript view and turn it is measuring.
 *
 * Both the scope and the key kind have to survive this read. A transport call
 * id is unique only inside the response that minted it -- `OpenAIStepReader`
 * restarts its counter per response, so `mi-1` recurs across conversations --
 * and `seg:<firstToolCallId>` reuses its stretch's first call id, so a card and
 * the stretch around it can carry the same string under different kinds.
 * Flattening either axis lets one reader's decision estimate another card.
 */
export const callFolds = (all: Record<string, Scope>, scope: string): CallFolds => {
  const callClosed = new Set<string>()
  const callOpen = new Set<string>()
  const full = new Set<string>()
  const segClosed = new Set<string>()
  const segOpen = new Set<string>()
  const here = all[scope]

  if (!here) {
    return { callClosed, callOpen, full, segClosed, segOpen }
  }

  for (const key of here.open) {
    if (key.startsWith('full:')) {
      full.add(key.slice(5))
    } else if (key.startsWith('call:')) {
      callOpen.add(key.slice(5))
    } else if (key.startsWith('seg:')) {
      segOpen.add(key.slice(4))
    }
  }

  for (const key of here.closed) {
    if (key.startsWith('call:')) {
      callClosed.add(key.slice(5))
    } else if (key.startsWith('seg:')) {
      segClosed.add(key.slice(4))
    }
  }

  return { callClosed, callOpen, full, segClosed, segOpen }
}

/** Test seam, and what a session switch uses to forget last session's folds. */
export const resetFolds = (): void => $folds.set({})
