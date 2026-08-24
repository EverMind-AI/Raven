# TUI DAG live node stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A running DAG node shows a live one-line tail of its sub-agent's message stream under its row, and clicking the row opens a fixed-height box holding the node's conversation trace drawn by the transcript's own renderer.

**Architecture:** Entirely client-side. `dag.node` already returns a running node's transcript from the live activity the runner publishes, and republishes the whole thing on every update -- so a poll is a complete snapshot, not a delta. A new hook polls it for running and expanded nodes into a module store; two pure functions turn the stored wire messages into a width-cut tail line and a rows-cut message slice; one new component owns the three states of the slot under a node row.

**Tech Stack:** TypeScript, React, `@hermes/ink` (vendored Ink fork at `ui-tui/packages/hermes-ink`), nanostores, vitest, `ink-testing-library`.

**Spec:** `docs/specs/2026-08-23-tui-dag-live-node-stream-design.md` -- read it first. Every task below argues from a section of it.

**Correction (2026-08-24):** Task 4's premise that only a run still inside a live turn needs polling proved false in live use -- `run_subagent_dag` returns as soon as it launches the run, so a node routinely outlives the turn that started it and `idle()` clears the live list at exactly that boundary. See spec section A for what the shipped poll actually merges and stops on.

## Global Constraints

- **Worktree:** `/Evermind/sh_evermind/xuedizhan/Raven/.claude/worktrees/feat+tui_dag_live_acp_stream`, branch `feat/tui_dag_live_acp_stream`, based on `feat/tui_dag_dependency_graph` at `1be8aafb`. Do not touch any other worktree -- other sessions are editing them.
- **Commits need the user's word.** AGENTS.md 3.4: a commit step written in a plan is *not* pre-authorization. Stage the work, run the tests, report -- the driver obtains authorization and commits. Never `git commit --amend`. Never push.
- **Commit messages:** Conventional Commits, ASCII only (no em-dash, curly quotes, ellipsis), header <= 100 chars, scope `tui`. Trailer `Co-authored-by: Claude (<your own model id>) <noreply@anthropic.com>`.
- **Comments:** English only. Do not add a comment that restates the code. New files carry a header comment in the house shape (see any file under `ui-tui/src/lib/`): SPDX line, copyright, then a blank comment line and prose explaining *why the file exists*.
- **No backend change, no RPC schema change, no client regeneration.** `dag.node` already returns everything read here. If a task seems to need a schema change, stop and report -- it means the plan is wrong.
- **Per-file test run:** `cd ui-tui && npm test -- src/__tests__/<file> --reporter=dot`
- **Full suite:** `cd ui-tui && npm test -- --no-file-parallelism`. The flag is required: ink render tests flake under default worker parallelism once the suite passes ~100 files.
- **Type-check:** `cd ui-tui && npm run type-check`. **Lint:** `cd ui-tui && npm run lint`.
- **Strings are raw English**, not `tr()` keys. `dagPanel.tsx` already uses raw strings and `lint:i18n` only checks the generated catalogue against `i18n/messages.json`; it does not scan source.

## File Structure

| File | Responsibility |
|---|---|
| `ui-tui/src/lib/text.ts` (modify) | gains `clipToWidthFromEnd`, the mirror of `clipToWidth` |
| `ui-tui/src/lib/dagStream.ts` (new) | the two pure derivations: a width-cut tail line, a rows-cut message slice |
| `ui-tui/src/app/dagNodeStore.ts` (new) | `runId/nodeId -> wire messages`, published by identity |
| `ui-tui/src/app/useDagNodePoll.ts` (new) | which nodes to poll, and when to stop |
| `ui-tui/src/components/dagNodeTrace.tsx` (new) | the slot under a node row: nothing, stream line, or box |
| `ui-tui/src/components/dagPanel.tsx` (modify) | render the slot; `NodePrompt` moves out |
| `ui-tui/src/lib/dagOpenNodes.ts` (modify) | what counts as expandable |
| `ui-tui/src/lib/virtualHeights.ts` (modify) | the slot's rows in the height model |
| `ui-tui/src/app/useMainApp.ts` (modify) | mount the poll; thread the open set to the estimator |
| `ui-tui/src/config/limits.ts` (modify) | the four constants |
| `ui-tui/src/lib/dagStatus.ts` (modify) | `/dag <node>` prints the trace |
| `ui-tui/CONTEXT.md` (modify) | define **Stream line** and **Trace box** |

Task order is a dependency order: 1 and 2 are pure functions with no imports from the rest, 3 and 4 are the data path, 5 consumes 1-4, 6 wires it in, 7 makes the height model agree, 8 is independent and can be done at any point.

---

### Task 1: The tail of a flattened message stream

Spec section *B*. Two pure functions, no React, no store.

**Files:**
- Modify: `ui-tui/src/lib/text.ts` (add `clipToWidthFromEnd` after `clipToWidth`, which ends at line 88)
- Create: `ui-tui/src/lib/dagStream.ts`
- Test: `ui-tui/src/__tests__/dagStream.test.ts`

**Interfaces:**
- Consumes: `stringWidth` from `@hermes/ink`; `WS_RE` and `compactPreview` already in `text.ts`; `formatToolCall` (`text.ts:236`); `callSubject` (`ui-tui/src/domain/episodeFold.ts:53`); `TranscriptMessage` from `../rpc/index.js`.
- Produces:
  - `clipToWidthFromEnd(raw: string, width: number): string`
  - `dagStreamTail(messages: readonly TranscriptMessage[], width: number): string`

- [ ] **Step 1: Write the failing tests**

Create `ui-tui/src/__tests__/dagStream.test.ts`:

```tsx
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it } from 'vitest'

import type { TranscriptMessage } from '../rpc/index.js'

import { dagStreamTail } from '../lib/dagStream.js'
import { clipToWidthFromEnd } from '../lib/text.js'

const say = (text: string): TranscriptMessage => ({ role: 'assistant', text })

describe('clipToWidthFromEnd', () => {
  it('returns a string that already fits, unmarked', () => {
    expect(clipToWidthFromEnd('short', 20)).toBe('short')
  })

  it('keeps the end and marks the cut at the front', () => {
    expect(clipToWidthFromEnd('abcdefghij', 5)).toBe('…ghij')
  })

  it('collapses newlines and runs of space so the result is one line', () => {
    expect(clipToWidthFromEnd('a\n\n  b\tc', 40)).toBe('a b c')
  })

  it('never half-includes a double-width cell', () => {
    // Four cells of budget, one reserved for the marker: three cells left,
    // which fits one wide char and not two.
    expect(clipToWidthFromEnd('一二三', 4)).toBe('…三')
  })

  it('is empty at a non-positive budget', () => {
    expect(clipToWidthFromEnd('abc', 0)).toBe('')
  })
})

describe('dagStreamTail', () => {
  it('is empty for no messages', () => {
    expect(dagStreamTail([], 40)).toBe('')
  })

  it('reads the last thing said when it fits', () => {
    expect(dagStreamTail([say('first'), say('second')], 40)).toBe('first · second')
  })

  it('cuts to the width, keeping the newest end', () => {
    expect(dagStreamTail([say('aaaaaaaaaa'), say('bbbb')], 6)).toBe('…bbbb')
  })

  it('names a tool call the way the transcript names it', () => {
    const msg: TranscriptMessage = {
      role: 'assistant',
      text: 'checking',
      tool_calls: [{ id: 'c1', name: 'read_file', arguments: '{"path":"a/b.ts"}' }]
    }

    expect(dagStreamTail([msg], 80)).toBe('checking · Read File("a/b.ts")')
  })

  it('carries a thought, then the text, then the calls, in that order', () => {
    const msg: TranscriptMessage = {
      role: 'assistant',
      reasoning_content: 'thinking',
      text: 'saying',
      tool_calls: [{ id: 'c1', name: 'bash', arguments: '{"command":"ls"}' }]
    }

    expect(dagStreamTail([msg], 80)).toBe('thinking · saying · Bash("ls")')
  })

  it('takes a tool result as the whole of that row', () => {
    const result: TranscriptMessage = { role: 'tool', text: '42 lines', tool_call_id: 'c1' }

    expect(dagStreamTail([result], 40)).toBe('42 lines')
  })

  it('skips a message that contributes nothing', () => {
    expect(dagStreamTail([say('a'), { role: 'assistant' }, say('b')], 40)).toBe('a · b')
  })

  it('reads only as far back as the width needs', () => {
    // The assertion that the walk is from the end: a huge transcript whose head
    // could never be shown must not be visited at all.
    let touched = 0
    const messages = Array.from({ length: 400 }, (_unused, i) => {
      const text = `m${i}`

      return new Proxy({ role: 'assistant', text } as TranscriptMessage, {
        get(target, prop) {
          touched++

          return target[prop as keyof TranscriptMessage]
        }
      })
    })

    dagStreamTail(messages, 10)

    // A handful of messages at the tail, each read for a few fields -- nowhere
    // near 400.
    expect(touched).toBeLessThan(60)
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui-tui && npm test -- src/__tests__/dagStream.test.ts --reporter=dot`
Expected: FAIL -- cannot resolve `../lib/dagStream.js`, and `clipToWidthFromEnd` is not exported from `text.ts`.

- [ ] **Step 3: Add `clipToWidthFromEnd` to `text.ts`**

Insert directly after `clipToWidth` (which closes with `return \`${out}…\`` and `}` at line 88):

```ts
// The mirror of `clipToWidth`: keep the *end* and mark the cut at the front.
// For a tail that is being refreshed in place, where the newest characters are
// the ones worth the cells.
export const clipToWidthFromEnd = (raw: string, width: number) => {
  const one = raw.replace(WS_RE, ' ').trim()

  if (width <= 0) {
    return ''
  }

  if (stringWidth(one) <= width) {
    return one
  }

  const chars = [...one]
  let out = ''
  let w = 0

  for (let i = chars.length - 1; i >= 0; i--) {
    const ch = chars[i]!
    const cw = stringWidth(ch)

    if (w + cw > width - 1) {
      break
    }

    out = ch + out
    w += cw
  }

  // The cut often lands on a space, and `… bbbb` spends a cell saying nothing.
  return `…${out.replace(/^ /, '')}`
}
```

- [ ] **Step 4: Create `ui-tui/src/lib/dagStream.ts`**

```ts
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// A DAG node's live transcript, reduced to what will fit on screen.
//
// Two reductions of the same wire messages, because the slot under a node row
// has two sizes: one line of the newest characters while the row is collapsed,
// and a slice of whole messages while it is expanded. Both are pure and take
// their budget as an argument -- the panel's width and its fixed row count are
// the only things that decide how much of a run is legible, and a reduction that
// read either from a store could not be tested against a known budget.

import { stringWidth } from '@hermes/ink'

import type { TranscriptMessage } from '../rpc/index.js'

import { callSubject } from '../domain/episodeFold.js'
import { clipToWidthFromEnd, formatToolCall } from './text.js'

const SEP = ' · '

// A cap on how much of one message is examined. A tool result can be megabytes
// and only the last cells of it can ever be shown, so the work must follow the
// budget rather than the payload. Four chars per cell is well clear of the worst
// real case (a double-width char is two cells for one char).
const CHARS_PER_CELL = 4

/** One message's contribution to the flattened stream, in wire order. */
const contribution = (msg: TranscriptMessage): string => {
  if (msg.role === 'tool') {
    return (msg.text ?? '').trim()
  }

  const parts = [msg.reasoning_content ?? '', msg.text ?? '']

  for (const call of msg.tool_calls ?? []) {
    parts.push(formatToolCall(call.name, callSubject(call.arguments)))
  }

  return parts
    .map(part => part.trim())
    .filter(Boolean)
    .join(SEP)
}

/**
 * The last `width` cells of everything the node has produced.
 *
 * Walked from the newest message backwards and stopped as soon as the budget is
 * covered: the transcript is capped at 400 messages server-side and this is
 * re-derived twice a second, so the cost has to follow the row width rather than
 * the run's length.
 */
export const dagStreamTail = (messages: readonly TranscriptMessage[], width: number): string => {
  if (width <= 0) {
    return ''
  }

  const budget = width * CHARS_PER_CELL
  let acc = ''

  for (let i = messages.length - 1; i >= 0; i--) {
    const whole = contribution(messages[i]!)

    if (!whole) {
      continue
    }

    // Slicing by code unit can orphan a low surrogate at the front; it would
    // render as a replacement char in the one position the marker occupies.
    const part = whole.length > budget ? whole.slice(-budget).replace(/^[\uDC00-\uDFFF]/, '') : whole

    acc = acc ? `${part}${SEP}${acc}` : part

    if (stringWidth(acc) >= width) {
      break
    }
  }

  return clipToWidthFromEnd(acc, width)
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd ui-tui && npm test -- src/__tests__/dagStream.test.ts --reporter=dot`
Expected: PASS, 14 tests.

If `Read File("a/b.ts")` does not match, read `callSubject` (`ui-tui/src/domain/episodeFold.ts:53`) and `formatToolCall` (`ui-tui/src/lib/text.ts:236`) and correct the *test expectation* to what those two actually produce -- they are existing, tested helpers and this task must not change them.

- [ ] **Step 6: Type-check and lint**

Run: `cd ui-tui && npm run type-check && npm run lint`
Expected: no errors. Warnings are pre-existing (the baseline is 22).

- [ ] **Step 7: Stage and report**

```bash
git add ui-tui/src/lib/dagStream.ts ui-tui/src/lib/text.ts ui-tui/src/__tests__/dagStream.test.ts
git status --short
```

Report the test counts. Do not commit -- the driver holds authorization (Global Constraints).

Commit message for the driver:

```
feat(tui): reduce a dag node's live transcript to one line of its newest text
```

---

### Task 2: The message slice that fits the box

Spec section *C*, "Tail-fitting is a pure function". Same module as Task 1, different budget: rows rather than cells.

**Files:**
- Modify: `ui-tui/src/lib/dagStream.ts`
- Modify: `ui-tui/src/config/limits.ts`
- Test: `ui-tui/src/__tests__/dagStream.test.ts`

**Interfaces:**
- Consumes: `estimatedMsgHeight` (`ui-tui/src/lib/virtualHeights.ts:82`), `Msg` from `../types.js`.
- Produces:
  - `fitTraceTail(msgs: readonly Msg[], rows: number, cols: number): { hidden: number; shown: Msg[] }`
  - `DAG_TRACE_ROWS = 8`, `DAG_TRACE_BOX_ROWS = 12`, `DAG_NODE_POLL_MS = 500`, `DAG_TRACE_OUTPUT_CHARS = 4000` in `config/limits.ts`

There is no import cycle here and there must not be one: `dagStream.ts` imports `virtualHeights.ts`, and Task 7 adds a *constant* to `virtualHeights.ts` rather than a call back into `dagStream.ts`. That is the reason the box's height is fixed.

- [ ] **Step 1: Write the failing tests**

Append to `ui-tui/src/__tests__/dagStream.test.ts` (and add `fitTraceTail` to the import from `../lib/dagStream.js`, plus `import type { Msg } from '../types.js'`):

```tsx
describe('fitTraceTail', () => {
  const line = (text: string): Msg => ({ role: 'assistant', text })

  it('shows everything when it all fits, hiding nothing', () => {
    const msgs = [line('a'), line('b')]
    const fit = fitTraceTail(msgs, 8, 60)

    expect(fit.shown).toEqual(msgs)
    expect(fit.hidden).toBe(0)
  })

  it('keeps the newest messages and counts what it dropped', () => {
    const msgs = Array.from({ length: 40 }, (_unused, i) => line(`m${i}`))
    const fit = fitTraceTail(msgs, 8, 60)

    expect(fit.shown.length).toBeLessThan(msgs.length)
    expect(fit.hidden).toBe(msgs.length - fit.shown.length)
    // The tail, not the head.
    expect(fit.shown.at(-1)).toBe(msgs.at(-1))
  })

  it('never exceeds the row budget', () => {
    const msgs = Array.from({ length: 40 }, (_unused, i) => line(`m${i}`))
    const fit = fitTraceTail(msgs, 8, 60)
    const used = fit.shown.reduce((n, msg) => n + estimatedMsgHeight(msg, 60, { compact: false, details: false }), 0)

    expect(used).toBeLessThanOrEqual(8)
  })

  it('still shows one message that is taller than the whole budget', () => {
    const huge = line(Array.from({ length: 200 }, (_unused, i) => `line ${i}`).join('\n'))
    const fit = fitTraceTail([line('older'), huge], 8, 60)

    expect(fit.shown).toEqual([huge])
    expect(fit.hidden).toBe(1)
  })

  it('is empty for no messages', () => {
    expect(fitTraceTail([], 8, 60)).toEqual({ hidden: 0, shown: [] })
  })
})
```

Add to the test file's imports: `import { estimatedMsgHeight } from '../lib/virtualHeights.js'`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui-tui && npm test -- src/__tests__/dagStream.test.ts --reporter=dot`
Expected: FAIL -- `fitTraceTail` is not exported.

- [ ] **Step 3: Add the four constants to `config/limits.ts`**

Follow the file's existing shape (a comment above each export explaining the number, as `DIRECT_STEP_POLL_MS` at line 28 does):

```ts
// One `dag.node` per running-or-expanded node per tick, so a little slower than
// the direct-chat poll, which is one call however many instances exist.
export const DAG_NODE_POLL_MS = 500

// Rows of trace inside an expanded node's box. Blank-padded when the trace is
// shorter, so the box's height never depends on its content.
export const DAG_TRACE_ROWS = 8

// The box's outer height: DAG_TRACE_ROWS plus a header, a footer, and the two
// border rows. `height` on a bordered Box is the outer height -- the fork sets a
// Yoga border -- so this is what the height model adds and what the Box takes.
export const DAG_TRACE_BOX_ROWS = DAG_TRACE_ROWS + 4

// `max_output_chars` for the trace read. A finished node's output arrives as the
// last message of the trace, so this bounds the answer the box can show; matches
// what `/dag <node>` already asks for.
export const DAG_TRACE_OUTPUT_CHARS = 4000
```

- [ ] **Step 4: Add `fitTraceTail` to `dagStream.ts`**

Add the imports:

```ts
import type { Msg } from '../types.js'

import { estimatedMsgHeight } from './virtualHeights.js'
```

and the function:

```ts
// What the box's own rendering does, so the fit is measured against the same
// thing it draws: a `MessageLine` with no details expanded.
const TRACE_ESTIMATE = { compact: false, details: false }

/**
 * The newest messages that fit `rows`, and how many were left above them.
 *
 * Measured with the transcript's own estimator rather than a second one, so a
 * change to how a message renders cannot make the box overflow the height the
 * virtualizer was told to expect.
 *
 * One message taller than the whole budget is still shown. Truncating it would
 * leave the box empty for exactly the run whose output a reader most wants, and
 * the Box clips the overflow either way.
 */
export const fitTraceTail = (
  msgs: readonly Msg[],
  rows: number,
  cols: number
): { hidden: number; shown: Msg[] } => {
  const shown: Msg[] = []
  let used = 0

  for (let i = msgs.length - 1; i >= 0; i--) {
    const msg = msgs[i]!
    const height = estimatedMsgHeight(msg, cols, TRACE_ESTIMATE)

    if (shown.length > 0 && used + height > rows) {
      break
    }

    shown.unshift(msg)
    used += height
  }

  return { hidden: msgs.length - shown.length, shown }
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd ui-tui && npm test -- src/__tests__/dagStream.test.ts --reporter=dot`
Expected: PASS, 19 tests.

- [ ] **Step 6: Type-check and lint**

Run: `cd ui-tui && npm run type-check && npm run lint`
Expected: no errors.

- [ ] **Step 7: Stage and report**

```bash
git add ui-tui/src/lib/dagStream.ts ui-tui/src/config/limits.ts ui-tui/src/__tests__/dagStream.test.ts
```

Commit message for the driver:

```
feat(tui): pick the newest dag trace messages that fit a fixed row budget
```

---

### Task 3: The trace store

Spec section *A*, last paragraph. A module store, because the panel has two render sites and the transcript remounts it -- the same reason `dagOpenNodes.ts` is one.

**Files:**
- Create: `ui-tui/src/app/dagNodeStore.ts`
- Test: `ui-tui/src/__tests__/dagNodeStore.test.ts`

**Interfaces:**
- Consumes: `atom` from `nanostores`; `TranscriptMessage` from `../rpc/index.js`.
- Produces:
  - `$dagNodeTraces: ReadableAtom<ReadonlyMap<string, readonly TranscriptMessage[]>>`
  - `setDagNodeTrace(key: string, messages: readonly TranscriptMessage[]): void`
  - `getDagNodeTrace(key: string): readonly TranscriptMessage[] | undefined`
  - `resetDagNodeTraces(): void`

Keys are `dagNodeKey(runId, nodeId)` from `ui-tui/src/lib/dagOpenNodes.ts:26`, reused rather than reinvented so the store and the open set are keyed alike.

- [ ] **Step 1: Write the failing test**

Create `ui-tui/src/__tests__/dagNodeStore.test.ts`:

```tsx
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { beforeEach, describe, expect, it } from 'vitest'

import type { TranscriptMessage } from '../rpc/index.js'

import {
  $dagNodeTraces,
  getDagNodeTrace,
  resetDagNodeTraces,
  setDagNodeTrace
} from '../app/dagNodeStore.js'

const say = (text: string): TranscriptMessage => ({ role: 'assistant', text })

beforeEach(() => {
  resetDagNodeTraces()
})

describe('dagNodeStore', () => {
  it('reads back what it stored', () => {
    setDagNodeTrace('r/a', [say('one')])

    expect(getDagNodeTrace('r/a')).toEqual([say('one')])
  })

  it('is undefined for a node never fetched', () => {
    expect(getDagNodeTrace('r/nope')).toBeUndefined()
  })

  it('publishes a new map so a subscriber re-renders', () => {
    const before = $dagNodeTraces.get()

    setDagNodeTrace('r/a', [say('one')])

    expect($dagNodeTraces.get()).not.toBe(before)
  })

  it('does not publish when the trace has not moved', () => {
    setDagNodeTrace('r/a', [say('one')])

    const after = $dagNodeTraces.get()

    setDagNodeTrace('r/a', [say('one')])

    // The poll re-reads twice a second and an idle agent returns the same
    // snapshot every time; republishing it would re-render every graph in the
    // transcript for no change.
    expect($dagNodeTraces.get()).toBe(after)
  })

  it('publishes when the newest message grew', () => {
    setDagNodeTrace('r/a', [say('one')])

    const after = $dagNodeTraces.get()

    setDagNodeTrace('r/a', [say('one and a half')])

    expect($dagNodeTraces.get()).not.toBe(after)
  })

  it('publishes when a message was appended', () => {
    setDagNodeTrace('r/a', [say('one')])

    const after = $dagNodeTraces.get()

    setDagNodeTrace('r/a', [say('one'), say('two')])

    expect($dagNodeTraces.get()).not.toBe(after)
  })

  it('empties on reset', () => {
    setDagNodeTrace('r/a', [say('one')])
    resetDagNodeTraces()

    expect(getDagNodeTrace('r/a')).toBeUndefined()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ui-tui && npm test -- src/__tests__/dagNodeStore.test.ts --reporter=dot`
Expected: FAIL -- cannot resolve `../app/dagNodeStore.js`.

- [ ] **Step 3: Create the store**

```ts
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// What each watched DAG node has said so far, as the wire returned it.
//
// A module store for the same reason `dagOpenNodes` is one: the panel has two
// render sites and a work segment re-renders its graph from a different branch
// once the turn settles, so state held in the component would be dropped at the
// moment a run finished.
//
// The wire messages are stored, not the folded rows, because the two readers of
// this want different reductions of them -- a line of characters and a slice of
// messages -- and folding here would make one of them reconstruct what folding
// threw away.

import { atom } from 'nanostores'

import type { TranscriptMessage } from '../rpc/index.js'

export const $dagNodeTraces = atom<ReadonlyMap<string, readonly TranscriptMessage[]>>(new Map())

// Enough of a trace to tell "it moved" from "the agent is between updates". The
// collector republishes its whole transcript on every frame, so an idle agent
// returns a byte-identical snapshot twice a second and a store that published it
// would re-render every graph in the transcript for nothing.
const signature = (messages: readonly TranscriptMessage[]) => {
  const last = messages.at(-1)

  return [
    messages.length,
    last?.role ?? '',
    last?.text?.length ?? 0,
    last?.reasoning_content?.length ?? 0,
    last?.tool_calls?.length ?? 0
  ].join(':')
}

export const getDagNodeTrace = (key: string) => $dagNodeTraces.get().get(key)

export const setDagNodeTrace = (key: string, messages: readonly TranscriptMessage[]) => {
  const current = $dagNodeTraces.get()
  const held = current.get(key)

  if (held && signature(held) === signature(messages)) {
    return
  }

  const next = new Map(current)

  next.set(key, messages)
  $dagNodeTraces.set(next)
}

export const resetDagNodeTraces = () => $dagNodeTraces.set(new Map())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ui-tui && npm test -- src/__tests__/dagNodeStore.test.ts --reporter=dot`
Expected: PASS, 7 tests.

- [ ] **Step 5: Clear the store on session reset**

Find where `resetFolds` (or the other per-transcript view state) is called on session reset -- search: `grep -rn "resetFolds\|resetDirectChat" ui-tui/src/app | grep -v __tests__`. Call `resetDagNodeTraces()` in the same place, for the same reason: a trace belongs to the conversation on screen, and a resumed one is a different conversation.

- [ ] **Step 6: Run the affected suites, type-check, lint**

Run: `cd ui-tui && npm test -- src/__tests__/dagNodeStore.test.ts --reporter=dot && npm run type-check && npm run lint`
Also run whatever test file covers the reset site you edited.
Expected: PASS, no type or lint errors.

- [ ] **Step 7: Stage and report**

```bash
git add ui-tui/src/app/dagNodeStore.ts ui-tui/src/__tests__/dagNodeStore.test.ts
git add -u ui-tui/src/app
```

Commit message for the driver:

```
feat(tui): hold each watched dag node's transcript in its own store
```

---

### Task 4: The poll

**Note:** this task's only-live-runs premise was superseded -- see the Correction note near the top of this document.

Spec section *A*. Mirrors `useDirectStepPoll` (`ui-tui/src/app/useDirectStepPoll.ts`) -- read that file first; it is the same problem solved once already, including why it is a read and not a stream.

**Files:**
- Create: `ui-tui/src/app/useDagNodePoll.ts`
- Test: `ui-tui/src/__tests__/dagNodePoll.test.tsx`

**Interfaces:**
- Consumes: `setDagNodeTrace`, `getDagNodeTrace` (Task 3); `dagNodeKey` (`lib/dagOpenNodes.ts:26`); `DagRunState` (`domain/dagRun.ts`); `DAG_NODE_POLL_MS`, `DAG_TRACE_OUTPUT_CHARS` (Task 2).
- Produces: `useDagNodePoll(rpc: Rpc, getSessionKey: () => null | string, runs: readonly DagRunState[], openKeys: ReadonlySet<string>): void`

Which nodes are read each tick:
- every node of `runs` whose `status` is `'running'`;
- every key in `openKeys` naming a node of `runs`, **unless** that node's status is terminal and the store already holds its trace -- a finished node is read once and then left alone.

- [ ] **Step 1: Write the failing test**

Create `ui-tui/src/__tests__/dagNodePoll.test.tsx`:

```tsx
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Whether anything ever *calls* the trace read, and whether it stops calling.
// The reductions and the store are covered on their own; this is the half that
// decides if a reader ever sees a moving line.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'

import { getDagNodeTrace, resetDagNodeTraces } from '../app/dagNodeStore.js'
import { useDagNodePoll } from '../app/useDagNodePoll.js'
import { DAG_NODE_POLL_MS } from '../config/limits.js'
import { dagNodeKey } from '../lib/dagOpenNodes.js'

const node = (id: string, status: DagRunNodeStatus) => ({ id, subagent: 'codex-acp', dependsOn: [], status })

const run = (...nodes: ReturnType<typeof node>[]): DagRunState => ({
  runId: 'r1',
  done: false,
  nodes
})

const answering = (messages: unknown[]) => {
  const calls: Record<string, unknown>[] = []
  const rpc = async (_method: string, params?: Record<string, unknown>) => {
    calls.push(params ?? {})

    return { node: { messages } } as never
  }

  return { calls, rpc: rpc as never }
}

const Probe = ({
  openKeys,
  rpc,
  runs
}: {
  openKeys: ReadonlySet<string>
  rpc: never
  runs: readonly DagRunState[]
}) => {
  useDagNodePoll(rpc, () => 's1', runs, openKeys)

  return null
}

const mount = (rpc: never, runs: readonly DagRunState[], openKeys: ReadonlySet<string> = new Set()) =>
  renderSync(<Probe openKeys={openKeys} rpc={rpc} runs={runs} />, { stdout: new PassThrough() as never })

beforeEach(() => {
  resetDagNodeTraces()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useDagNodePoll', () => {
  it('reads a running node at once, without waiting for a tick', async () => {
    const { calls, rpc } = answering([{ role: 'assistant', text: 'hi' }])

    mount(rpc, [run(node('a', 'running'))])
    await vi.advanceTimersByTimeAsync(0)

    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({ node: 'a', run_id: 'r1', session_key: 's1' })
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))).toEqual([{ role: 'assistant', text: 'hi' }])
  })

  it('keeps reading it on the interval', async () => {
    const { calls, rpc } = answering([{ role: 'assistant', text: 'hi' }])

    mount(rpc, [run(node('a', 'running'))])
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 2 + 10)

    expect(calls.length).toBeGreaterThanOrEqual(3)
  })

  it('reads nothing when no node is running and none is open', async () => {
    const { calls, rpc } = answering([])

    mount(rpc, [run(node('a', 'completed'))])
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 2)

    expect(calls).toHaveLength(0)
  })

  it('reads an expanded finished node once and then stops', async () => {
    const { calls, rpc } = answering([{ role: 'assistant', text: 'done' }])

    mount(rpc, [run(node('a', 'completed'))], new Set([dagNodeKey('r1', 'a')]))
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 3 + 10)

    expect(calls).toHaveLength(1)
  })

  it('ignores an open key that names no node of any live run', async () => {
    const { calls, rpc } = answering([])

    mount(rpc, [run(node('a', 'completed'))], new Set([dagNodeKey('other', 'z')]))
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS)

    expect(calls).toHaveLength(0)
  })

  it('stops on unmount', async () => {
    const { calls, rpc } = answering([{ role: 'assistant', text: 'hi' }])
    const app = mount(rpc, [run(node('a', 'running'))])

    await vi.advanceTimersByTimeAsync(0)
    const seen = calls.length

    app.unmount()
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 3)

    expect(calls).toHaveLength(seen)
  })

  it('survives a read that throws', async () => {
    const rpc = (async () => {
      throw new Error('run dir is gone')
    }) as never

    mount(rpc, [run(node('a', 'running'))])
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS + 10)

    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))).toBeUndefined()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ui-tui && npm test -- src/__tests__/dagNodePoll.test.tsx --reporter=dot`
Expected: FAIL -- cannot resolve `../app/useDagNodePoll.js`.

- [ ] **Step 3: Create the hook**

```ts
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Keeping a watched DAG node's transcript up to date while it works.
//
// The counterpart of `useDirectStepPoll`, and a read rather than a stream for
// the same reason: no event carries a node's steps. `dag.node` falls back to the
// activity the runner is publishing when nothing has reached disk yet, and the
// collector republishes its whole transcript on every frame -- so each response
// is a complete snapshot and a reader can poll it holding no state at all.
//
// Only live runs are polled. A *running* node exists only inside a turn in
// flight; a resumed run is finished or interrupted by definition, so there is
// nothing there to watch.

import { useEffect, useMemo } from 'react'

import type { DagRunState } from '../domain/dagRun.js'
import type { DagNodeResult } from '../rpc/index.js'

import { DAG_NODE_POLL_MS, DAG_TRACE_OUTPUT_CHARS } from '../config/limits.js'
import { dagNodeKey } from '../lib/dagOpenNodes.js'
import { getDagNodeTrace, setDagNodeTrace } from './dagNodeStore.js'

/** Structurally `GatewayRpc`, restated so a test can pass a plain function. */
type Rpc = <T extends object>(
  method: string,
  params?: Record<string, unknown>,
  opts?: { quiet?: boolean }
) => Promise<null | T>

const RUNNING: DagRunState['nodes'][number]['status'] = 'running'

export const useDagNodePoll = (
  rpc: Rpc,
  getSessionKey: () => null | string,
  runs: readonly DagRunState[],
  openKeys: ReadonlySet<string>
): void => {
  const targets = useMemo(() => {
    const out: { nodeId: string; running: boolean; runId: string }[] = []

    for (const run of runs) {
      for (const node of run.nodes) {
        const running = node.status === RUNNING

        if (running || openKeys.has(dagNodeKey(run.runId, node.id))) {
          out.push({ nodeId: node.id, running, runId: run.runId })
        }
      }
    }

    return out
  }, [openKeys, runs])

  // A stable dependency: `targets` is a fresh array every render, and keying the
  // effect on it would clear and rebuild the interval before it ever ticked.
  // `running` is part of it so the interval rearms when a node stops.
  const signature = targets.map(t => `${t.runId}/${t.nodeId}:${t.running ? 1 : 0}`).join(',')

  useEffect(() => {
    if (targets.length === 0) {
      return
    }

    let live = true

    const read = async ({ nodeId, runId }: { nodeId: string; runId: string }) => {
      try {
        // `quiet` is required rather than cosmetic: a pruned run dir answers
        // with an error, and without this it would print into the transcript --
        // which is the surface this is trying to keep readable.
        const result = await rpc<DagNodeResult>(
          'dag.node',
          {
            max_output_chars: DAG_TRACE_OUTPUT_CHARS,
            node: nodeId,
            run_id: runId,
            session_key: getSessionKey() ?? undefined
          },
          { quiet: true }
        )
        const messages = result?.node?.messages

        if (live && messages && messages.length > 0) {
          setDagNodeTrace(dagNodeKey(runId, nodeId), messages)
        }
      } catch {
        // A node whose read fails keeps whatever it had; the row falls back to
        // its prompt template, which is what it showed before this existed.
      }
    }

    const tick = () => {
      for (const target of targets) {
        // An expanded node that has stopped is read once: its trace cannot
        // change again, and re-reading it would poll a finished run forever.
        // Checked per tick rather than when `targets` was built -- that memo
        // does not recompute when the store gains a trace, so a filter up there
        // would never see the read it exists to suppress.
        if (!target.running && getDagNodeTrace(dagNodeKey(target.runId, target.nodeId)) !== undefined) {
          continue
        }

        void read(target)
      }
    }

    // At once as well as on the interval: a node that starts running between
    // ticks would otherwise show nothing for half a second.
    tick()

    const id = setInterval(tick, DAG_NODE_POLL_MS)

    return () => {
      live = false
      clearInterval(id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `signature` stands
    // in for `targets`; see above.
  }, [getSessionKey, rpc, signature])
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ui-tui && npm test -- src/__tests__/dagNodePoll.test.tsx --reporter=dot`
Expected: PASS, 7 tests.

If the eslint disable comment is rejected by `npm run lint`, read the repo's own precedent -- `grep -rn "exhaustive-deps" ui-tui/src | grep -v __tests__` -- and match whatever that does instead.

- [ ] **Step 5: Type-check and lint**

Run: `cd ui-tui && npm run type-check && npm run lint`
Expected: no errors.

- [ ] **Step 6: Stage and report**

```bash
git add ui-tui/src/app/useDagNodePoll.ts ui-tui/src/__tests__/dagNodePoll.test.tsx
```

Commit message for the driver:

```
feat(tui): poll a running dag node's transcript while it works
```

---

### Task 5: The slot under a node row

Spec sections *B* and *C*. One component owning all three states, so `dagPanel` gains one element and not a conditional tree.

**Files:**
- Create: `ui-tui/src/components/dagNodeTrace.tsx`
- Test: `ui-tui/src/__tests__/dagNodeTrace.test.tsx`

**Interfaces:**
- Consumes: `dagStreamTail`, `fitTraceTail` (Tasks 1-2); `$dagNodeTraces` (Task 3); `DAG_TRACE_ROWS`, `DAG_TRACE_BOX_ROWS` (Task 2); `toTranscriptMessages` (`domain/messages.ts:54`); `MessageLine` (`components/messageLine.tsx:73`); `Spinner` (`components/thinking.tsx:160`); `dagNodeKey` (`lib/dagOpenNodes.ts:26`); `DagRunNode` (`domain/dagRun.ts`).
- Produces: `DagNodeSlot({ node, ordinal, open, runId, t, width }): JSX.Element | null`

`NodePrompt` **moves here** from `dagPanel.tsx:43` (delete it there). It is now the box's fallback rather than the whole expanded state, and leaving it in `dagPanel` would need `dagPanel` to import this file while this file imports that one.

State table:

| `open` | node status | renders |
|---|---|---|
| false | `running` | the stream line |
| false | anything else | `null` |
| true | any | the box: the trace when one is stored, else `NodePrompt` |

- [ ] **Step 1: Write the failing test**

Create `ui-tui/src/__tests__/dagNodeTrace.test.tsx`:

```tsx
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { render } from 'ink-testing-library'
import React from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import type { DagRunNode, DagRunNodeStatus } from '../domain/dagRun.js'
import type { TranscriptMessage } from '../rpc/index.js'

import { resetDagNodeTraces, setDagNodeTrace } from '../app/dagNodeStore.js'
import { DagNodeSlot } from '../components/dagNodeTrace.js'
import { DAG_TRACE_BOX_ROWS } from '../config/limits.js'
import { dagNodeKey } from '../lib/dagOpenNodes.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const node = (status: DagRunNodeStatus, promptTemplate = 'audit the journal writer'): DagRunNode => ({
  dependsOn: [],
  id: 'n3f8a2',
  promptTemplate,
  status,
  subagent: 'codex-acp'
})

const slot = (over: { node: DagRunNode; open?: boolean }) =>
  stripAnsi(
    render(
      <DagNodeSlot
        node={over.node}
        open={over.open ?? false}
        ordinal={2}
        runId="r1"
        t={DEFAULT_THEME}
        width={64}
      />
    ).lastFrame() ?? ''
  )

const say = (text: string): TranscriptMessage => ({ role: 'assistant', text })

beforeEach(() => {
  resetDagNodeTraces()
})

describe('DagNodeSlot', () => {
  it('draws nothing for a finished node that is not expanded', () => {
    expect(slot({ node: node('completed') }).trim()).toBe('')
  })

  it('draws a placeholder for a running node with nothing said yet', () => {
    expect(slot({ node: node('running') })).toContain('working')
  })

  it('draws the tail of the stream for a running node', () => {
    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), [say('the journal writes one frame per line')])

    expect(slot({ node: node('running') })).toContain('per line')
  })

  it('keeps the stream line to one row', () => {
    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), [say('x'.repeat(400))])

    const rows = slot({ node: node('running') }).split('\n').filter(Boolean)

    expect(rows).toHaveLength(1)
  })

  it('draws the trace in a box when expanded', () => {
    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), [say('the answer')])

    const frame = slot({ node: node('running'), open: true })

    expect(frame).toContain('the answer')
    expect(frame).toContain('n3f8a2')
  })

  it('holds the box to its fixed height whatever the trace length', () => {
    const short = [say('one line')]
    const long = Array.from({ length: 60 }, (_unused, i) => say(`message number ${i}`))

    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), short)
    const shortRows = slot({ node: node('running'), open: true }).split('\n').length

    resetDagNodeTraces()
    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), long)
    const longRows = slot({ node: node('running'), open: true }).split('\n').length

    expect(shortRows).toBe(DAG_TRACE_BOX_ROWS)
    expect(longRows).toBe(DAG_TRACE_BOX_ROWS)
  })

  it('says how many messages it could not show', () => {
    setDagNodeTrace(
      dagNodeKey('r1', 'n3f8a2'),
      Array.from({ length: 60 }, (_unused, i) => say(`message number ${i}`))
    )

    expect(slot({ node: node('running'), open: true })).toContain('earlier')
  })

  it('points at /dag with the ordinal when nothing was cut', () => {
    setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), [say('one line')])

    expect(slot({ node: node('running'), open: true })).toContain('/dag 2')
  })

  it('falls back to the prompt template when no trace was fetched', () => {
    expect(slot({ node: node('completed'), open: true })).toContain('audit the journal writer')
  })

  it('shows the node id in the fallback too, since /dag takes it', () => {
    expect(slot({ node: node('completed'), open: true })).toContain('n3f8a2')
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ui-tui && npm test -- src/__tests__/dagNodeTrace.test.tsx --reporter=dot`
Expected: FAIL -- cannot resolve `../components/dagNodeTrace.js`.

- [ ] **Step 3: Create the component**

```tsx
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The slot under one DAG node's row: nothing, a live line, or a box.
//
// Three states of one place rather than three places, because the second click
// has to read as "go back" -- a box that appeared *beside* the line it replaced
// would leave the reader looking for what changed.
//
// The box is a constant height, and both halves of that are deliberate: its
// footer is drawn whether or not anything was cut, and a short trace is
// blank-padded. A box that grew as the run produced messages would shove every
// row beneath it several times a second, and the height model could then not
// state a panel's height without folding the trace first.

import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { memo, useMemo } from 'react'

import type { DagRunNode } from '../domain/dagRun.js'
import type { Theme } from '../theme.js'

import { $dagNodeTraces } from '../app/dagNodeStore.js'
import { DAG_TRACE_BOX_ROWS, DAG_TRACE_ROWS } from '../config/limits.js'
import { toTranscriptMessages } from '../domain/messages.js'
import { dagStreamTail, fitTraceTail } from '../lib/dagStream.js'
import { dagNodeKey } from '../lib/dagOpenNodes.js'
import { MessageLine } from './messageLine.js'
import { Spinner } from './thinking.js'

const INDENT = 2

// The spinner and the space after it.
const SPINNER_CELLS = 2

// Bounds the fallback block. The template is shown as authored, where a
// `{{ ref:<path> }}` is thirty-odd literal characters rather than the file, so
// reaching this takes a genuinely long instruction block.
const PROMPT_CHARS = 4000

/** What the node was asked, for a node whose trace could not be read. */
const NodePrompt = ({ node, t, width }: { node: DagRunNode; t: Theme; width: number }) => {
  const prompt = node.promptTemplate ?? ''

  return (
    <Box flexDirection="column" width={Math.max(8, width)}>
      <Text color={t.color.muted} dim>
        {node.id}
        {node.outputFile ? ` → ${node.outputFile}` : ''}
      </Text>
      <Text color={t.color.text} wrap="wrap">
        {prompt.length > PROMPT_CHARS ? `${prompt.slice(0, PROMPT_CHARS)}\n…` : prompt}
      </Text>
    </Box>
  )
}

// No `wrap="truncate-end"` here, deliberately: the Spinner is a nested <Text>,
// and a nested Text makes ink's own truncation a no-op (see the note on
// `clipToWidth`, text.ts:63). `dagStreamTail` has already cut to `room`, which
// is the actual guarantee that this stays one row.
const StreamLine = ({ tail, t, width }: { tail: string; t: Theme; width: number }) => (
  <Box paddingLeft={INDENT} width={Math.max(8, width)}>
    <Text color={t.color.muted}>
      <Spinner color={t.color.accent} variant="tool" /> {tail || 'working…'}
    </Text>
  </Box>
)

export const DagNodeSlot = memo(function DagNodeSlot({
  node,
  ordinal,
  open,
  runId,
  t,
  width
}: {
  node: DagRunNode
  ordinal: number
  open: boolean
  runId: string
  t: Theme
  width: number
}) {
  const traces = useStore($dagNodeTraces)
  const messages = traces.get(dagNodeKey(runId, node.id))

  const room = Math.max(8, width - INDENT - SPINNER_CELLS)
  const tail = useMemo(() => (messages ? dagStreamTail(messages, room) : ''), [messages, room])

  // Borders take a column each side, and the box is indented from the row.
  const inner = Math.max(24, width - INDENT - 2)
  const traceMsgs = useMemo(() => (messages ? toTranscriptMessages(messages) : []), [messages])
  const fit = useMemo(() => fitTraceTail(traceMsgs, DAG_TRACE_ROWS, inner), [inner, traceMsgs])

  if (!open) {
    return node.status === 'running' ? <StreamLine t={t} tail={tail} width={width} /> : null
  }

  return (
    <Box
      borderColor={t.color.border}
      borderStyle="round"
      flexDirection="column"
      height={DAG_TRACE_BOX_ROWS}
      marginLeft={INDENT}
      overflow="hidden"
      width={Math.max(28, width - INDENT)}
    >
      <Text color={t.color.muted} dim wrap="truncate-end">
        {node.id} · {node.subagent}
        {fit.shown.length > 0 ? ` · ${traceMsgs.length} msgs` : ''}
        {node.outputFile ? ` → ${node.outputFile}` : ''}
      </Text>

      {fit.shown.length > 0 ? (
        <Box flexDirection="column" flexGrow={1}>
          {fit.shown.map((msg, index) => (
            <MessageLine cols={inner} key={index} msg={msg} t={t} />
          ))}
        </Box>
      ) : (
        <Box flexDirection="column" flexGrow={1}>
          <NodePrompt node={node} t={t} width={inner} />
        </Box>
      )}

      <Text color={t.color.muted} dim wrap="truncate-end">
        {fit.hidden > 0 ? `↑ ${fit.hidden} earlier messages — /dag ${ordinal}` : `/dag ${ordinal} for the full trace`}
      </Text>
    </Box>
  )
})
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ui-tui && npm test -- src/__tests__/dagNodeTrace.test.tsx --reporter=dot`
Expected: PASS, 11 tests.

Two failures are likely and both are the plan being wrong about layout, not the test:
- **The box is 14 rows, not 12.** Then `height` is the *content* height in this fork after all. Fix `DAG_TRACE_BOX_ROWS` to `DAG_TRACE_ROWS + 2`, re-read `packages/hermes-ink/src/ink/styles.ts:697-708`, and say so in the report -- Task 7 depends on this number.
- **`flexGrow={1}` does not pad a short trace to the full height.** Then render explicit blank `<Text> </Text>` rows: `DAG_TRACE_ROWS - usedRows` of them, where `usedRows` is the sum of `estimatedMsgHeight` over `fit.shown`. Keep the height constant either way -- that is the requirement, not the mechanism.

- [ ] **Step 5: Type-check and lint**

Run: `cd ui-tui && npm run type-check && npm run lint`
Expected: no errors.

- [ ] **Step 6: Stage and report**

```bash
git add ui-tui/src/components/dagNodeTrace.tsx ui-tui/src/__tests__/dagNodeTrace.test.tsx
```

Report the box's real row count -- Task 7 needs it.

Commit message for the driver:

```
feat(tui): draw a dag node's live stream, and its trace in a fixed box
```

---

### Task 6: Wire the slot into the panel

Spec section *C*, "What is expandable changes".

**Files:**
- Modify: `ui-tui/src/components/dagPanel.tsx` (delete `NodePrompt` at 43-58 and `PROMPT_INDENT`/`PROMPT_CHARS`; render `DagNodeSlot` where `{open && toggle && <NodePrompt .../>}` is)
- Modify: `ui-tui/src/lib/dagOpenNodes.ts:31` (`dagNodeToggleKey`)
- Modify: `ui-tui/CONTEXT.md`
- Test: `ui-tui/src/__tests__/dagPanel.test.tsx`, `ui-tui/src/__tests__/dagOpenNodes.test.ts` if one exists (check first)

**Interfaces:**
- Consumes: `DagNodeSlot` (Task 5).
- Produces: no new exports. `dagNodeToggleKey` keeps its signature and widens its answer.

`NodeRow` needs the node's ordinal, which it already takes as `ordinal`. Pass it straight through.

- [ ] **Step 1: Write the failing tests**

Append to `ui-tui/src/__tests__/dagPanel.test.tsx`:

```tsx
describe('DagPanel node slot', () => {
  it('draws a stream line under a running node without a click', () => {
    const f = frame(<DagPanel run={DIAMOND} t={DEFAULT_THEME} />)

    expect(f).toContain('working…')
  })

  it('expands a node that has no prompt template', () => {
    // Before the trace existed, the template was the only thing a click could
    // reveal, so a node without one was not clickable. There is a trace now.
    const bare: DagRunState = { ...DIAMOND, nodes: [node('solo', 'completed')] }

    expect(dagNodeToggleKey('r1', bare.nodes[0]!)).toBe(dagNodeKey('r1', 'solo'))
  })

  it('still refuses a pending node with nothing to show', () => {
    expect(dagNodeToggleKey('r1', node('later', 'pending'))).toBeNull()
  })

  it('expands a pending node that does carry a template', () => {
    expect(dagNodeToggleKey('r1', { ...node('later', 'pending'), promptTemplate: 'do it' })).not.toBeNull()
  })
})
```

Add `dagNodeToggleKey` to the existing import from `../lib/dagOpenNodes.js`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui-tui && npm test -- src/__tests__/dagPanel.test.tsx --reporter=dot`
Expected: FAIL -- no `working…` in the frame; `dagNodeToggleKey` returns `null` for a template-less node.

- [ ] **Step 3: Widen `dagNodeToggleKey`**

Replace the function at `ui-tui/src/lib/dagOpenNodes.ts:31` and rewrite its docstring -- the old one says the click reveals a prompt, which stops being true here:

```ts
/**
 * The key a click on this node opens, or `null` when it opens nothing.
 *
 * The one rule, so a node's row and its box in the picture cannot disagree about
 * what is expandable. A node that has started has a trace to show whether or not
 * the call's arguments carried a template; one still pending with no template
 * has neither, and an affordance that swallows a click is worse than none.
 */
export const dagNodeToggleKey = (runId: string, node: DagRunNode): string | null =>
  node.promptTemplate || node.status !== 'pending' ? dagNodeKey(runId, node.id) : null
```

- [ ] **Step 4: Render the slot in `dagPanel.tsx`**

Delete `PROMPT_INDENT`, `PROMPT_CHARS` and the whole `NodePrompt` component (they now live in `dagNodeTrace.tsx`), add `import { DagNodeSlot } from './dagNodeTrace.js'`, and replace the last line of `NodeRow`'s returned tree:

```tsx
      {open && toggle && <NodePrompt node={node} t={t} width={width} />}
```

with:

```tsx
      <DagNodeSlot
        node={node}
        open={open && Boolean(toggle)}
        ordinal={ordinal}
        runId={runId}
        t={t}
        width={width}
      />
```

Update the file's header comment: it currently says clicking "expands the prompt in full", which is no longer what a click does.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd ui-tui && npm test -- src/__tests__/dagPanel.test.tsx src/__tests__/dagNodeTrace.test.tsx --reporter=dot`
Expected: PASS.

Existing `dagPanel` tests that assert on an expanded prompt will now see a box. Update them to the new behaviour -- that is the point of the change, not a regression -- but keep any test that asserts a row and its picture box toggle *together*; that invariant must survive.

- [ ] **Step 6: Define the two terms in `ui-tui/CONTEXT.md`**

Add beside the existing **DAG Panel** entry, in the same shape as its neighbours:

```markdown
**Stream line** -- the single row under a running DAG node's row, carrying the
last line's worth of what its sub-agent has produced, refreshed while it works.
Not a step ticker: it is a character tail, so it moves.
_Avoid_: "log line", "tail row".

**Trace box** -- the fixed-height bordered block a DAG node row expands into,
holding the node's conversation trace drawn by the transcript's own renderer.
Replaces the stream line rather than joining it, and is the same height whatever
the trace's length.
_Avoid_: "detail panel", "node output".
```

- [ ] **Step 7: Full suite, type-check, lint**

Run: `cd ui-tui && npm test -- --no-file-parallelism 2>&1 | tail -20 && npm run type-check && npm run lint`
Expected: no failures. Compare the pass count against the baseline on `feat/tui_dag_dependency_graph` (1465 passed, 13 skipped) plus this plan's new tests.

- [ ] **Step 8: Stage and report**

```bash
git add ui-tui/src/components/dagPanel.tsx ui-tui/src/lib/dagOpenNodes.ts ui-tui/CONTEXT.md ui-tui/src/__tests__/
```

Commit message for the driver:

```
feat(tui): give a dag node row its live slot, and widen what a click opens
```

---

### Task 7: Make the height model agree with what is drawn

Spec section *E*. Without this the virtualizer positions rows above the viewport by an estimate that is short by up to twelve rows per expanded node.

**Files:**
- Modify: `ui-tui/src/lib/virtualHeights.ts` (`dagPanelRows` at 72, `dagSig` at 38, `estimatedMsgHeight`'s options at 82, the two `dagPanelRows` calls at 148 and 153)
- Modify: `ui-tui/src/app/useMainApp.ts` (`estimateRowHeight` at 332)
- Test: `ui-tui/src/__tests__/virtualHeights.test.ts`

**Interfaces:**
- Consumes: `DAG_TRACE_BOX_ROWS` (Task 2 -- use the number Task 5 reported, not the one the plan guessed); `dagNodeKey` (`lib/dagOpenNodes.ts:26`); `$dagOpenNodes`.
- Produces: `estimatedMsgHeight(msg, cols, opts)` gains `dagOpen?: ReadonlySet<string>`, defaulting to an empty set so every existing caller and test keeps working unchanged.

**Do not** put the open set into `messageHeightKey`. That key is also a message's identity (`useMainApp.ts:260`), and an identity that changes on a click would make a toggle look like a different row. `dagSig` does gain a running-node count, which is a property of the run.

- [ ] **Step 1: Write the failing tests**

Append to `ui-tui/src/__tests__/virtualHeights.test.ts` (follow the file's existing helpers for building an episodes `Msg` with a `dag` on a tool -- read it first; do not invent a second shape):

```tsx
describe('dag panel height', () => {
  it('counts a stream line for each running node', () => {
    const idle = msgWithDag(runWith([node('a', 'completed')]))
    const busy = msgWithDag(runWith([node('a', 'running')]))

    expect(estimatedMsgHeight(busy, 100, BASE)).toBe(estimatedMsgHeight(idle, 100, BASE) + 1)
  })

  it('counts the box for an expanded node instead of the line', () => {
    const msg = msgWithDag(runWith([node('a', 'running')]))
    const open = new Set([dagNodeKey('r1', 'a')])

    expect(estimatedMsgHeight(msg, 100, { ...BASE, dagOpen: open })).toBe(
      estimatedMsgHeight(msg, 100, BASE) - 1 + DAG_TRACE_BOX_ROWS
    )
  })

  it('is unchanged for a run with nothing running and nothing open', () => {
    const msg = msgWithDag(runWith([node('a', 'completed')]))

    expect(estimatedMsgHeight(msg, 100, { ...BASE, dagOpen: new Set() })).toBe(
      estimatedMsgHeight(msg, 100, BASE)
    )
  })

  it('re-keys when a node starts running', () => {
    const idle = msgWithDag(runWith([node('a', 'pending')]))
    const busy = msgWithDag(runWith([node('a', 'running')]))

    expect(messageHeightKey(busy)).not.toBe(messageHeightKey(idle))
  })
})
```

`BASE` is `{ compact: false, details: false }`. Define `msgWithDag`/`runWith`/`node` from the existing helpers in that file if it has them, or add them locally.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui-tui && npm test -- src/__tests__/virtualHeights.test.ts --reporter=dot`
Expected: FAIL -- heights unchanged by running/open state; `dagOpen` is not an accepted option.

- [ ] **Step 3: Teach `dagPanelRows` about the slot**

```ts
const dagPanelRows = (
  run: NonNullable<EpisodeTool['dag']>,
  width: number,
  open: ReadonlySet<string>
): number => {
  if (run.nodes.length === 0) {
    return 0
  }

  const picture = layoutDagGraph(run.nodes, { width })

  // What `DagNodeSlot` draws under each row: a box when expanded, one live line
  // while it runs, nothing otherwise. A constant for the box, which is why the
  // box is a fixed height -- reading its real height here would make this
  // estimator depend on the fold it is estimating.
  const slots = run.nodes.reduce(
    (rows, node) =>
      rows +
      (open.has(dagNodeKey(run.runId, node.id))
        ? DAG_TRACE_BOX_ROWS
        : node.status === 'running'
          ? 1
          : 0),
    0
  )

  return 1 + (picture?.height ?? 0) + run.nodes.length + slots + (run.done && run.dir ? 1 : 0)
}
```

Add the imports: `DAG_TRACE_BOX_ROWS` from `../config/limits.js`, `dagNodeKey` from `./dagOpenNodes.js`.

- [ ] **Step 4: Thread the option through**

In `estimatedMsgHeight`'s options type add `dagOpen?: ReadonlySet<string>` and destructure it with `dagOpen = EMPTY_OPEN`, where `const EMPTY_OPEN: ReadonlySet<string> = new Set()` sits at module scope (one allocation, not one per call). Pass `dagOpen` as the third argument at both call sites (lines 148 and 153).

In `messageHeightKey`, extend `dagSig`:

```ts
  const dagSig = (dag: EpisodeTool['dag']) =>
    dag
      ? `/${dag.nodes.length}.${dag.done ? 1 : 0}.${dag.dir ? 1 : 0}.${dag.nodes.filter(node => node.status === 'running').length}`
      : ''
```

- [ ] **Step 5: Read the open set in `useMainApp`**

Add `import { useStore } from '@nanostores/react'` if it is not already there, `import { $dagOpenNodes } from '../lib/dagOpenNodes.js'`, then above `estimateRowHeight` (line ~332):

```ts
  const dagOpen = useStore($dagOpenNodes)
```

and inside the callback's options object add `dagOpen,` -- plus `dagOpen` to the `useCallback` dependency array.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd ui-tui && npm test -- src/__tests__/virtualHeights.test.ts --reporter=dot`
Expected: PASS.

- [ ] **Step 7: Prove the estimate matches what renders**

This is the assertion that fails if either side changes alone. Add to `ui-tui/src/__tests__/dagNodeTrace.test.tsx`:

```tsx
it('renders exactly the rows the height model budgets for a box', () => {
  setDagNodeTrace(dagNodeKey('r1', 'n3f8a2'), [say('one line')])

  const rows = slot({ node: node('running'), open: true }).split('\n').length

  expect(rows).toBe(DAG_TRACE_BOX_ROWS)
})
```

Run: `cd ui-tui && npm test -- src/__tests__/dagNodeTrace.test.tsx --reporter=dot`
Expected: PASS. If it fails, the constant is wrong, not the test -- fix `DAG_TRACE_BOX_ROWS` and re-run Task 7's tests.

- [ ] **Step 8: Full suite, type-check, lint**

Run: `cd ui-tui && npm test -- --no-file-parallelism 2>&1 | tail -20 && npm run type-check && npm run lint`
Expected: no failures.

- [ ] **Step 9: Stage and report**

```bash
git add ui-tui/src/lib/virtualHeights.ts ui-tui/src/app/useMainApp.ts ui-tui/src/__tests__/
```

Commit message for the driver:

```
fix(tui): count a dag node's live slot in the transcript height model
```

---

### Task 8: Mount the poll

Spec section *A*. Independent of Tasks 5-7 in code, but pointless before Task 3-4 exist.

**Files:**
- Modify: `ui-tui/src/app/useMainApp.ts` (beside `useDirectStepPoll` at line 570)
- Test: covered by Task 4's hook test plus the full suite; no new test file.

**Interfaces:**
- Consumes: `useDagNodePoll` (Task 4); `rpc` (`useMainApp.ts:403`); `sidRef` (`useMainApp.ts:281`); `$dagOpenNodes`; the live runs from `turnStore`.

- [ ] **Step 1: Find the live runs**

`turnStore`'s state carries `dagRuns: DagRunState[]` (`ui-tui/src/app/turnStore.ts:82`). Read it with the store's own selector hook, the way line 147 does:

```ts
  const dagRuns = useTurnSelector(state => state.dagRuns)
```

Check whether `useTurnSelector` compares by identity; if it does, this is already stable between turns and needs nothing more. If it re-renders on every turn-state change, that is acceptable here -- the hook's own effect is keyed on a signature string, not on the array.

- [ ] **Step 2: Mount the hook**

Directly after the `useDirectStepPoll` call at line 570:

```ts
  // A watched DAG node is re-read while it works; see `useDagNodePoll` for why
  // that is a read rather than a stream.
  useDagNodePoll(rpc, sidRef, dagRuns, dagOpen)
```

`dagOpen` is the `useStore($dagOpenNodes)` added in Task 7. If Task 7 has not landed, add it here and leave it for Task 7 to reuse.

- [ ] **Step 3: Verify it actually polls**

Run the full suite plus a type-check:

`cd ui-tui && npm test -- --no-file-parallelism 2>&1 | tail -20 && npm run type-check && npm run lint`

Then confirm by inspection that `dagRuns` is non-empty during a live DAG turn -- `grep -n "dagRuns" ui-tui/src/app/turnController.ts` shows where it is filled. If the poll would only ever see an empty array, stop and report: the plan named the wrong source.

- [ ] **Step 4: Stage and report**

```bash
git add ui-tui/src/app/useMainApp.ts
```

Commit message for the driver:

```
feat(tui): watch a live dag run's nodes from the main app
```

---

### Task 9: `/dag <node>` prints the trace

Spec section *D*. Independent of every other task -- it can be done first if that is convenient.

**Files:**
- Modify: `ui-tui/src/lib/dagStatus.ts` (`formatDagNodeDetail` at 151)
- Test: `ui-tui/src/__tests__/dagStatus.test.ts`

**Interfaces:**
- Consumes: `DagNodeDetail` (already imported there); `formatToolCall` (`lib/text.ts:236`), `callSubject` (`domain/episodeFold.ts:53`).
- Produces: `formatDagNodeDetail` keeps its signature.

Without this the box is the only view of a trace anywhere in the TUI, and a 60-message run has 50 messages reachable by no means at all.

- [ ] **Step 1: Write the failing test**

Append to `ui-tui/src/__tests__/dagStatus.test.ts`:

```tsx
describe('formatDagNodeDetail trace', () => {
  const base = {
    node: 'n1',
    output_chars: 3,
    output_truncated: false,
    run_id: 'r1'
  }

  it('prints the steps between the prompt and the output', () => {
    const text = formatDagNodeDetail({
      ...base,
      messages: [
        { role: 'assistant', text: 'looking', tool_calls: [{ id: 'c', name: 'read_file', arguments: '{"path":"a.ts"}' }] },
        { role: 'tool', text: '12 lines', tool_call_id: 'c' }
      ],
      output: 'ok',
      prompt: 'do it'
    } as never)

    expect(text).toContain('do it')
    expect(text).toContain('looking')
    expect(text).toContain('a.ts')
    expect(text).toContain('12 lines')
    expect(text).toContain('ok')
  })

  it('prints what it printed before when there are no messages', () => {
    const text = formatDagNodeDetail({ ...base, messages: [], output: 'ok', prompt: 'do it' } as never)

    expect(text).toContain('prompt:')
    expect(text).toContain('output:')
    expect(text).not.toContain('trace:')
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ui-tui && npm test -- src/__tests__/dagStatus.test.ts --reporter=dot`
Expected: FAIL -- no `looking`, no `12 lines`.

- [ ] **Step 3: Print the trace**

Add above `formatDagNodeDetail`:

```ts
// One line per thing the node did, for the command that is the only way to read
// a trace longer than the panel's box. Flat text on purpose: this goes into the
// transcript as a system message, which has no structure to render into.
const traceLines = (messages: DagNodeDetail['messages']): string[] => {
  const out: string[] = []

  for (const msg of messages) {
    if (msg.reasoning_content?.trim()) {
      out.push(`  (thought) ${compactPreview(msg.reasoning_content, 200)}`)
    }

    if (msg.text?.trim() && msg.role !== 'tool') {
      out.push(`  ${compactPreview(msg.text, 300)}`)
    }

    for (const call of msg.tool_calls ?? []) {
      out.push(`  > ${formatToolCall(call.name, callSubject(call.arguments))}`)
    }

    if (msg.role === 'tool' && msg.text?.trim()) {
      out.push(`    ${compactPreview(msg.text, 200)}`)
    }
  }

  return out
}
```

and thread it into the returned array, between the prompt and the output:

```ts
export const formatDagNodeDetail = (detail: DagNodeDetail): string => {
  const size = detail.output_truncated
    ? ` (${detail.output_chars} chars, truncated)`
    : detail.output_chars > 0
      ? ` (${detail.output_chars} chars)`
      : ''
  const trace = traceLines(detail.messages ?? [])

  return [
    `── ${detail.node} @ ${detail.run_id}${size} ──`,
    'prompt:',
    detail.prompt ?? '  (no prompt — the node never ran)',
    ...(trace.length > 0 ? ['trace:', ...trace] : []),
    'output:',
    detail.output ?? '  (no output)'
  ].join('\n')
}
```

Import `compactPreview`, `formatToolCall` from `./text.js` and `callSubject` from `../domain/episodeFold.js` if they are not already imported.

The prompt is printed from `detail.prompt`, not from the trace's first message, even though the server puts it there too -- `/dag`'s existing output shape is what its users read, and changing it is not this task.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ui-tui && npm test -- src/__tests__/dagStatus.test.ts --reporter=dot`
Expected: PASS.

- [ ] **Step 5: Type-check and lint**

Run: `cd ui-tui && npm run type-check && npm run lint`
Expected: no errors.

- [ ] **Step 6: Stage and report**

```bash
git add ui-tui/src/lib/dagStatus.ts ui-tui/src/__tests__/dagStatus.test.ts
```

Commit message for the driver:

```
feat(tui): print a dag node's steps in the command that reads it
```

---

## Final verification

Run after every task lands. This is the whole gate, and every line of it must be green before the branch is offered for review.

- [ ] `cd ui-tui && npm test -- --no-file-parallelism` -- no failures. Baseline on `feat/tui_dag_dependency_graph` is 1465 passed / 13 skipped; this plan adds roughly 50 tests.
- [ ] `cd ui-tui && npm run type-check` -- clean.
- [ ] `cd ui && npm run type-check` -- clean. No RPC type changed here, but this is the client that fails *silently* when one does, so it is cheap insurance.
- [ ] `cd ui-tui && npm run lint` -- 0 errors (22 warnings is the baseline).
- [ ] `cd ui-tui && npm run lint:rpc && npm run lint:i18n` -- both clean; neither should have moved.
- [ ] `uv run pytest -q` from the repo root -- unchanged. No Python was touched; run it to prove that rather than assert it.
- [ ] `make check-large-files` -- exit 0.
- [ ] `cd ui-tui && npm run build` -- the TUI runs a prebuilt gitignored bundle (`ui-tui/dist/entry.js`), so without this the user tests stale code.
- [ ] **Watch it work.** The panel is live-event-only: `/resume` never redraws a graph, so seeing the stream line move costs a real `run_subagent_dag` over an ACP agent. Run one with two nodes, one depending on the other, and confirm: the line under the running node moves; clicking the row opens the box; clicking again returns the line; the rows below do not jump while the box is open.

## Notes for whoever runs this

- **The bundle.** `raven tui` loads `ui-tui/dist/entry.js`, which is gitignored and stale until `npm run build`. A change that "does nothing" is usually this.
- **Width behind a pipe.** Piping the TUI's stdout through `tee` makes Ink render at 80 columns regardless of the real terminal, which silently voids any layout check. Keep stdout on the tty and redirect stderr instead.
- **Do not run the full ui-tui suite in parallel.** See Global Constraints.
