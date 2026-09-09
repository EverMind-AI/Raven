// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

// StreamingMd — incremental markdown renderer for in-flight assistant text.
//
// Naive approach (render <Md text={full}/>) re-tokenizes the entire message
// on every stream delta. At 20-char batches over a 3 KB response that's 150
// full re-parses.
//
// This splits `text` at the last stable top-level block boundary (blank
// line outside a fenced code span) into:
//   stablePrefix — passed to an inner <Md>, memoized on its exact text
//                  value. During the turn, the prefix only grows monotonically,
//                  so its memo key matches the previous render and React
//                  reuses the cached subtree — zero re-tokenization.
//   unstableSuffix — the in-flight block(s). A separate <Md> re-parses just
//                    this tail on every delta (O(unstable length) vs.
//                    O(total length)).
//
// The boundary is stored in a ref so it only advances — idempotent under
// StrictMode double-render. Component unmounts between turns (isStreaming
// flips off → message moves to history and renders via <Md> directly), so
// the ref resets naturally.
//
// Layout: the two <Md> subtrees MUST render stacked (column). The parent
// container in messageLine.tsx is a default `flexDirection: 'row'` Box
// (Ink's default), so returning a bare Fragment of two <Md> siblings
// laid them out side-by-side — producing the "two jumbled columns while
// streaming" rendering bug. Wrapping in a flexDirection="column" Box
// here localizes the fix to the streaming path; the non-streaming <Md>
// already returns its own column Box, so its single-child case was never
// affected.

import { Box } from '@hermes/ink'
import { memo, useRef } from 'react'

import type { Theme } from '../theme.js'

import { Md } from './markdown.js'

// Track ``` / ~~~ and `$$` / `\[…\]` fences in one forward scan. Splitting
// inside one would orphan the opener and let the unstable suffix render as
// broken markdown. Math fences only toggle while code is closed, so examples
// containing math syntax inside code do not affect the boundary.
//
// NB: this is INTENTIONALLY more conservative than `markdown.tsx`'s
// parser, which falls back to paragraph rendering when an `$$` opener
// has no matching closer. The renderer can do that safely because it
// always sees the full text on every call. The streaming chunker
// cannot — once a chunk is committed to the monotonic stable prefix it
// is frozen, so prematurely deciding "this `$$` is just prose" would
// permanently commit a paragraph rendering that becomes wrong the
// instant the closer streams in. Treating any unmatched `$$` opener
// as still-open keeps the boundary parked behind it until the closer
// arrives (or the stream ends and the non-streaming `<Md>` takes over,
// at which point the renderer's fallback kicks in correctly).
// Find the last exact "\n\n" boundary outside a fenced block. This is O(n):
// the former backwards search re-scanned the whole prefix for every candidate
// boundary and became quadratic for long streaming responses.
export const findStableBoundary = (text: string) => {
  let codeOpen = false
  let mathOpen = false
  let mathOpener: '$$' | '\\[' | null = null
  let lastSafe = -1
  let i = 0

  while (i < text.length) {
    const nl = text.indexOf('\n', i)

    if (nl < 0) {
      break
    }

    const rawLine = text.slice(i, nl)
    const line = rawLine.trim()

    if (/^(?:`{3,}|~{3,})/.test(line)) {
      codeOpen = !codeOpen
    } else if (!codeOpen) {
      if (!mathOpen && /^\$\$/.test(line)) {
        const isSingleLine = line.length >= 4 && /\$\$$/.test(line)

        if (!isSingleLine) {
          mathOpen = true
          mathOpener = '$$'
        }
      } else if (!mathOpen && /^\\\[/.test(line)) {
        const isSingleLine = /\\\]$/.test(line)

        if (!isSingleLine) {
          mathOpen = true
          mathOpener = '\\['
        }
      } else if (mathOpen && mathOpener === '$$' && /\$\$$/.test(line)) {
        mathOpen = false
        mathOpener = null
      } else if (mathOpen && mathOpener === '\\[' && /\\\]$/.test(line)) {
        mathOpen = false
        mathOpener = null
      }
    }

    // An exact blank separator is represented by an empty physical line;
    // preserve the old `\n\n` semantics (whitespace-only lines are not a
    // stable boundary) while recording only candidates outside fences.
    if (rawLine.length === 0 && i > 0 && text[i - 1] === '\n' && !codeOpen && !mathOpen) {
      lastSafe = nl + 1
    }

    i = nl + 1
  }

  return lastSafe
}

export const StreamingMd = memo(function StreamingMd({ compact, t, text }: StreamingMdProps) {
  const stablePrefixRef = useRef('')

  // Reset if the text no longer starts with our recorded prefix (defensive;
  // normally the component unmounts between turns so this shouldn't trigger).
  if (!text.startsWith(stablePrefixRef.current)) {
    stablePrefixRef.current = ''
  }

  const boundary = findStableBoundary(text)

  // Only advance the prefix — never retreat. The boundary math looks at the
  // FULL text each call; if it returns a larger index than before, we grow
  // the cached prefix. Monotonic growth makes the memo key stable across
  // deltas (identical string → same <Md> subtree → no re-render).
  if (boundary > stablePrefixRef.current.length) {
    stablePrefixRef.current = text.slice(0, boundary)
  }

  const stablePrefix = stablePrefixRef.current
  const unstableSuffix = text.slice(stablePrefix.length)

  if (!stablePrefix) {
    return <Md compact={compact} t={t} text={unstableSuffix} />
  }

  if (!unstableSuffix) {
    return <Md compact={compact} t={t} text={stablePrefix} />
  }

  return (
    <Box flexDirection="column">
      <Md compact={compact} t={t} text={stablePrefix} />
      <Md compact={compact} t={t} text={unstableSuffix} />
    </Box>
  )
})

interface StreamingMdProps {
  compact?: boolean
  t: Theme
  text: string
}
