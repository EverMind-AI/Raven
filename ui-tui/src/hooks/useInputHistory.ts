import { useRef, useState } from 'react'

import * as inputHistory from '../lib/history.js'

/**
 * True once the text in the composer no longer matches the history entry it
 * was recalled from. A recalled entry is only navigation state while it is
 * untouched: typing over it turns the edited text into an ordinary draft, so
 * the next arrow key must not step to a neighbouring entry and discard it.
 *
 * Upstream keeps this in `useInputHandlers.ts`; here it lives beside the
 * history hook because the update-input wrapper in `useComposerState.ts` is
 * what has to apply it, and that module must not pull in the input handlers'
 * component graph just for one predicate.
 */
export function shouldDetachEditedHistoryInput(historyIdx: null | number, history: readonly string[], value: string) {
  return historyIdx !== null && value !== history[historyIdx]
}

export function useInputHistory() {
  const historyRef = useRef<string[]>(inputHistory.load())
  const [historyIdx, setHistoryIdx] = useState<number | null>(null)
  const historyDraftRef = useRef('')

  return { historyRef, historyIdx, setHistoryIdx, historyDraftRef, pushHistory: inputHistory.append }
}
