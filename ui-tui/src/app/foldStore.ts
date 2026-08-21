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

/** Open fold ids, per scope. A scope is one transcript view. */
export const $folds = atom<Record<string, readonly string[]>>({})

export const isFoldOpen = (scope: string, key: string): boolean => ($folds.get()[scope] ?? []).includes(key)

export const openFolds = (scope: string): readonly string[] => $folds.get()[scope] ?? []

export const toggleFold = (scope: string, key: string): void => {
  const all = $folds.get()
  const current = all[scope] ?? []

  $folds.set({
    ...all,
    [scope]: current.includes(key) ? current.filter(k => k !== key) : [...current, key]
  })
}

/** Test seam, and what a session switch uses to forget last session's folds. */
export const resetFolds = (): void => $folds.set({})
