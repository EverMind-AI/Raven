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

/** Test seam, and what a session switch uses to forget last session's folds. */
export const resetFolds = (): void => $folds.set({})
