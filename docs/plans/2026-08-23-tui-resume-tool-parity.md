# Tool-call parity across resume (TUI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A resumed session's tool-call rows behave exactly as a live turn's do -- same rows, same folds, same detail blocks, same clicks, same DAG graph.

**Architecture:** `episodeView` is already the only renderer for tool rows; resume simply never builds `Episode[]` for it. Extract the folding core that `directEpisodes.ts` already uses to build episodes from *stored* rows, give the resume payload an adapter onto it, and hydrate DAG runs before the transcript is set. Fold ids come from the transport's own call ids, so resumed and live transcripts share one key space.

**Tech Stack:** TypeScript + React (Ink fork `@hermes/ink`), nanostores, vitest. Python 3.12 + pydantic for the one RPC field.

**Spec:** `docs/specs/2026-08-23-tui-resume-tool-parity-design.md`

## Global Constraints

- Run tests with `npx vitest run --no-file-parallelism` from `ui-tui/`. The default worker parallelism flakes this suite; serial is the supported way.
- Python: `uv run pytest`, never bare `pytest`.
- Comments in English only. Do not add a comment where neighbouring lines have none. Explain *why*, never *what*.
- Do not add dependencies. Do not commit unless the user asks.
- Every new file needs a module docstring/header comment stating its purpose, plus the three SPDX lines the sibling files carry.
- Parity is measured against the live path: resumed rows set `ok: true` and never render `diff`, because the live typed transport has neither (`ToolCompletePayload` has no error field; `onToolComplete` discards `payload.diff`). Do not "improve" on this.
- Test files already exist for every area touched. Extend them; do not create parallel files. The one new test file is `episodeFold.test.ts`.

---

### Task 1: Extract the episode folding core

`foldDirectTurns` already folds stored rows into `kind: 'episodes'` messages. Pull its body into a shared module so the resume adapter in Task 2 uses the same code, and prove the direct-chat behaviour is unchanged.

**Files:**
- Create: `ui-tui/src/domain/episodeFold.ts`
- Modify: `ui-tui/src/domain/directEpisodes.ts`
- Test: `ui-tui/src/__tests__/episodeFold.test.ts` (create), `ui-tui/src/__tests__/directEpisodes.test.ts` (regression gate, unchanged)

**Interfaces:**
- Consumes: `Episode`, `EpisodeTool`, `Msg` from `../types.js`
- Produces:
  - `interface FoldRow { role: 'user' | 'assistant' | 'tool'; text: string; foldSeed?: string; reasoning?: string; calls?: readonly FoldCall[]; toolCallId?: string; atMs?: number; durationMs?: number; ok?: boolean }`
  - `interface FoldCall { id: string; name: string; arguments: string }`
  - `foldRowsIntoEpisodes(rows: readonly FoldRow[]): Msg[]`

- [ ] **Step 1: Write the failing test**

Create `ui-tui/src/__tests__/episodeFold.test.ts`:

```ts
import { describe, expect, it } from 'vitest'

import type { FoldRow } from '../domain/episodeFold.js'

import { foldRowsIntoEpisodes } from '../domain/episodeFold.js'

const row = (over: Partial<FoldRow> & Pick<FoldRow, 'role'>): FoldRow => ({ text: '', ...over })

describe('foldRowsIntoEpisodes', () => {
  it('builds one episodes message per turn, with a tool per call', () => {
    const msgs = foldRowsIntoEpisodes([
      row({ role: 'user', text: 'do it' }),
      row({
        role: 'assistant',
        calls: [{ arguments: '{"path":"a.ts"}', id: 'c1', name: 'read_file' }],
        foldSeed: 'c1'
      }),
      row({ durationMs: 1200, role: 'tool', text: 'contents', toolCallId: 'c1' }),
      row({ role: 'assistant', text: 'done' })
    ])

    expect(msgs[0]).toEqual({ role: 'user', text: 'do it' })

    const turn = msgs[1]!

    expect(turn.kind).toBe('episodes')
    expect(turn.text).toBe('done')

    const tool = turn.episodes![0]!.tools[0]!

    expect(tool).toMatchObject({ durationMs: 1200, id: 'c1', name: 'read_file', resultPreview: 'contents', summary: 'a.ts' })
  })

  it('leaves a call unanswered when no tool row matches it', () => {
    // An interrupted turn stores the call and never its result. The row still
    // has to appear, or the transcript loses the fact that it was made.
    const msgs = foldRowsIntoEpisodes([
      row({ role: 'assistant', calls: [{ arguments: '{}', id: 'c9', name: 'exec' }], foldSeed: 'c9' })
    ])

    expect(msgs[0]!.episodes![0]!.tools[0]).toMatchObject({ id: 'c9', resultPreview: undefined })
  })

  it('keeps a thought that produced no call', () => {
    const msgs = foldRowsIntoEpisodes([row({ reasoning: 'thinking', role: 'assistant' })])

    expect(msgs[0]!.episodes![0]).toMatchObject({ reasoning: 'thinking', tools: [] })
  })

  it('derives a duration from wall clocks when none was supplied', () => {
    // The direct-chat records carry two timestamps and no duration; the resume
    // payload carries a duration and no start. One core has to serve both.
    const msgs = foldRowsIntoEpisodes([
      row({ atMs: 1000, role: 'assistant', calls: [{ arguments: '{}', id: 'c1', name: 'exec' }], foldSeed: 'c1' }),
      row({ atMs: 1750, role: 'tool', text: 'ok', toolCallId: 'c1' })
    ])

    expect(msgs[0]!.episodes![0]!.tools[0]!.durationMs).toBe(750)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui-tui && npx vitest run src/__tests__/episodeFold.test.ts --no-file-parallelism`
Expected: FAIL — `Failed to resolve import "../domain/episodeFold.js"`

- [ ] **Step 3: Write the core**

Create `ui-tui/src/domain/episodeFold.ts`:

```ts
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Stored rows folded into the transcript's episode shape.
//
// Two callers reach this: a direct chat's instance log, and the main session's
// resume payload. They arrive in different wire shapes and mean the same thing,
// so each adapts to `FoldRow` and the folding itself happens once. A second
// implementation would drift, and the drift would show up as a resumed
// transcript that folds or expands differently from the live one -- which is
// the whole property this module exists to hold.

import type { Episode, EpisodeTool, Msg } from '../types.js'

import { callSubject } from './directEpisodes.js'

export interface FoldCall {
  arguments: string
  id: string
  name: string
}

export interface FoldRow {
  /** Wall clock, when the source has one. Used only to derive a duration. */
  atMs?: number
  calls?: readonly FoldCall[]
  /** Known call duration. Preferred over deriving one from `atMs`. */
  durationMs?: number
  /** The id a fold keys on, seeded from the first call of the turn. */
  foldSeed?: string
  ok?: boolean
  reasoning?: string
  role: 'assistant' | 'tool' | 'user'
  text: string
  toolCallId?: string
}

/**
 * Fold rows into transcript messages.
 *
 * A user row opens a turn and the assistant rows until the next one close it,
 * so the result is one `kind: 'episodes'` message per turn rather than per row:
 * `segmentTurn`'s fold spans episode boundaries, and a run of six reads only
 * collapses to "read 6 files" when they arrive in one message.
 */
export const foldRowsIntoEpisodes = (rows: readonly FoldRow[]): Msg[] => {
  const msgs: Msg[] = []
  let episodes: Episode[] = []
  let answer = ''
  // Carried onto the message and used as its fold key. A turn still running is
  // handed over as a fresh object every few hundred milliseconds, so a fold
  // keyed on the object closes itself; the call ids hold still.
  let foldId: string | undefined
  const pending = new Map<string, EpisodeTool>()

  const flush = () => {
    if (episodes.length || answer) {
      msgs.push({ episodes, foldId, kind: 'episodes', role: 'assistant', text: answer })
    }

    episodes = []
    answer = ''
    foldId = undefined
    pending.clear()
  }

  for (const row of rows) {
    if (row.role === 'user') {
      flush()
      msgs.push({ role: 'user', text: row.text })

      continue
    }

    foldId ??= row.foldSeed

    if (row.role === 'tool') {
      const tool = row.toolCallId ? pending.get(row.toolCallId) : undefined

      if (tool) {
        tool.resultPreview = row.text
        tool.ok = row.ok ?? true

        const derived = row.atMs && tool.startedAt ? Math.max(0, row.atMs - tool.startedAt) : undefined
        const durationMs = row.durationMs ?? derived

        if (durationMs !== undefined) {
          tool.durationMs = durationMs
        }
      }

      continue
    }

    const reasoning = row.reasoning?.trim() ?? ''
    const calls = row.calls ?? []

    if (!calls.length) {
      // No call to hang an episode on. Prose here is the turn's answer; a row
      // carrying only a thought still gets an episode, or the last thought
      // before the reply would vanish.
      if (row.text.trim()) {
        answer = answer ? `${answer}\n\n${row.text.trim()}` : row.text.trim()
      } else if (reasoning) {
        episodes.push({ index: episodes.length, reasoning, tools: [] })
      }

      continue
    }

    const tools = calls.map((call): EpisodeTool => {
      const tool: EpisodeTool = {
        done: true,
        id: call.id,
        name: call.name,
        ok: true,
        summary: callSubject(call.arguments),
        ...(row.atMs ? { startedAt: row.atMs } : {})
      }

      pending.set(call.id, tool)

      return tool
    })

    episodes.push({
      index: episodes.length,
      reasoning,
      // The runtime opens a calling row with empty content, so narration is
      // only set when the agent really did say something before acting.
      ...(row.text.trim() ? { narration: row.text.trim() } : {}),
      tools
    })
  }

  flush()

  return msgs
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui-tui && npx vitest run src/__tests__/episodeFold.test.ts --no-file-parallelism`
Expected: PASS, 4 tests.

- [ ] **Step 5: Rewrite `foldDirectTurns` as an adapter**

In `ui-tui/src/domain/directEpisodes.ts`, delete the body of `foldDirectTurns` (from `const msgs: Msg[] = []` to its closing `return msgs`) along with the now-unused `FAILED`/`readResult` helpers only if nothing else references them, and replace the function with:

```ts
/**
 * Fold an instance's stored rows into transcript messages.
 *
 * The runtime marks a failed call by prefixing its result, which is also how it
 * reaches the record on disk -- there is no separate status field on the row, so
 * the prefix is stripped here rather than in the shared core, where the resume
 * payload has no such convention.
 */
export const foldDirectTurns = (turns: DirectTurn[]): Msg[] =>
  foldRowsIntoEpisodes(
    turns.map((turn): FoldRow => {
      const failed = turn.role === 'tool' && turn.content.startsWith(FAILED)

      return {
        atMs: turn.at_ms,
        role: turn.role,
        text: failed ? turn.content.slice(FAILED.length).trim() : turn.content,
        ...(turn.reasoning_content ? { reasoning: turn.reasoning_content } : {}),
        ...(turn.tool_calls ? { calls: turn.tool_calls } : {}),
        ...(turn.call_id ? { foldSeed: turn.call_id } : {}),
        ...(turn.tool_call_id ? { toolCallId: turn.tool_call_id } : {}),
        ...(turn.role === 'tool' ? { ok: !failed } : {})
      }
    })
  )
```

Add the imports at the top of the file:

```ts
import type { FoldRow } from './episodeFold.js'

import { foldRowsIntoEpisodes } from './episodeFold.js'
```

Keep `const FAILED = '[failed]'` and its comment. Delete `readResult` if the file no longer calls it.

- [ ] **Step 6: Run the direct-chat regression gate**

Run: `cd ui-tui && npx vitest run src/__tests__/directEpisodes.test.ts src/__tests__/episodeFold.test.ts --no-file-parallelism`
Expected: PASS. `directEpisodes.test.ts` is unmodified — it is the proof the extraction changed no behaviour. If any case fails, the adapter is wrong, not the test.

- [ ] **Step 7: Type-check and commit**

```bash
cd ui-tui && npm run type-check && cd ..
git add ui-tui/src/domain/episodeFold.ts ui-tui/src/domain/directEpisodes.ts ui-tui/src/__tests__/episodeFold.test.ts
git commit -m "refactor(tui): one folding core behind the direct-chat episode builder"
```

---

### Task 2: Resume builds real episodes

**Files:**
- Modify: `ui-tui/src/domain/messages.ts:41-100` (`toTranscriptMessages`), `:116-124` (`TranscriptRow`)
- Test: `ui-tui/src/__tests__/messages.test.ts`

**Interfaces:**
- Consumes: `foldRowsIntoEpisodes`, `FoldRow` from Task 1
- Produces: `toTranscriptMessages(rows: unknown): Msg[]` — same signature, now returning `kind: 'episodes'` messages where the payload has tool calls

- [ ] **Step 1: Write the failing test**

Append to `ui-tui/src/__tests__/messages.test.ts`:

```ts
describe('toTranscriptMessages: resumed tool calls', () => {
  const RESUMED = [
    { role: 'user', text: 'read it' },
    {
      role: 'assistant',
      text: '',
      tool_calls: [{ arguments: '{"path":"a.ts"}', id: 'call-1', name: 'read_file' }]
    },
    { duration_ms: 1200, name: 'read_file', role: 'tool', text: 'contents', tool_call_id: 'call-1' },
    { role: 'assistant', text: 'here it is' }
  ]

  it('rebuilds an episode per call instead of a flat trail line', () => {
    const msgs = toTranscriptMessages(RESUMED)
    const turn = msgs.find(m => m.kind === 'episodes')!

    expect(turn.text).toBe('here it is')

    const tool = turn.episodes![0]!.tools[0]!

    expect(tool).toMatchObject({
      done: true,
      durationMs: 1200,
      id: 'call-1',
      name: 'read_file',
      resultPreview: 'contents',
      summary: 'a.ts'
    })
  })

  it('draws no clock when the stored row predates duration_ms', () => {
    // Absent means unknown. A zero would claim the call ran for no time.
    const msgs = toTranscriptMessages([
      { role: 'assistant', text: '', tool_calls: [{ arguments: '{}', id: 'call-1', name: 'exec' }] },
      { name: 'exec', role: 'tool', text: 'ok', tool_call_id: 'call-1' }
    ])

    expect(msgs.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!.durationMs).toBeUndefined()
  })

  it('still replaces a runtime-opened user row with the delivered line', () => {
    const msgs = toTranscriptMessages([{ origin: 'raven-code', role: 'user', text: 'internal prose' }])

    expect(msgs[0]!.role).toBe('system')
    expect(msgs[0]!.text).toContain('raven-code')
    expect(msgs[0]!.text).not.toContain('internal prose')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui-tui && npx vitest run src/__tests__/messages.test.ts --no-file-parallelism`
Expected: FAIL — the first two cases find no `kind: 'episodes'` message (`Cannot read properties of undefined`), because tool rows still become trail strings.

- [ ] **Step 3: Widen `TranscriptRow`**

Replace the interface at `ui-tui/src/domain/messages.ts:116`:

```ts
interface TranscriptToolCallRow {
  arguments?: string
  id?: string
  name?: string
}

interface TranscriptRow {
  context?: string
  duration_ms?: number
  name?: string
  /** See `GatewayTranscriptMessage.origin`: set when the runtime opened the turn. */
  origin?: string
  reasoning_content?: string
  role?: string
  text?: string
  tool_call_id?: string
  tool_calls?: TranscriptToolCallRow[]
}
```

- [ ] **Step 4: Rebuild `toTranscriptMessages` on the core**

Replace the body of `toTranscriptMessages` (`messages.ts:41`) with:

```ts
export const toTranscriptMessages = (rows: unknown): Msg[] => {
  if (!Array.isArray(rows)) {
    return []
  }

  const folded: FoldRow[] = []

  for (const row of rows) {
    if (!row || typeof row !== 'object') {
      continue
    }

    const {
      duration_ms: durationMs,
      origin,
      reasoning_content: reasoning,
      role,
      text,
      tool_call_id: toolCallId,
      tool_calls: toolCalls
    } = row as TranscriptRow

    if (role === 'user' && origin) {
      /* A turn the runtime opened, not a person typing. Its text is internal
         prose, so it is replaced rather than shown: the same sentence the live
         trail prints when a delegated result rejoins the conversation, which is
         also the row this replay was missing -- it arrives on an event, and an
         event is not in the transcript. */
      folded.push({ role: 'user', text: '' })
      folded.push({ role: 'assistant', text: `${origin} ${t('gui.deleg.delivered', 'delivered')}` })

      continue
    }

    if (role === 'tool') {
      folded.push({
        role: 'tool',
        text: typeof text === 'string' ? text : '',
        ...(durationMs != null ? { durationMs } : {}),
        ...(toolCallId ? { toolCallId } : {})
      })

      continue
    }

    if (role !== 'assistant' && role !== 'user') {
      continue
    }

    const calls = (toolCalls ?? [])
      .filter((call): call is Required<TranscriptToolCallRow> => Boolean(call?.id && call.name))
      .map(call => ({ arguments: call.arguments ?? '', id: call.id, name: call.name }))

    folded.push({
      role,
      text: typeof text === 'string' ? text : '',
      ...(calls.length ? { calls, foldSeed: calls[0]!.id } : {}),
      ...(reasoning ? { reasoning } : {})
    })
  }

  return foldRowsIntoEpisodes(folded)
}
```

Add the imports:

```ts
import type { FoldRow } from './episodeFold.js'

import { foldRowsIntoEpisodes } from './episodeFold.js'
```

Delete the now-unused `buildToolTrailLine` import if `messages.ts` no longer references it.

Note the delegated row: the core has no "system" role, so the delivered line is pushed as an assistant row after an empty user row, which flushes the previous turn exactly as the old code's `pending = []` did.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ui-tui && npx vitest run src/__tests__/messages.test.ts --no-file-parallelism`
Expected: PASS, including every pre-existing case in the file.

- [ ] **Step 6: Run the whole suite and commit**

```bash
cd ui-tui && npx vitest run --no-file-parallelism && npm run type-check && npm run lint && cd ..
git add ui-tui/src/domain/messages.ts ui-tui/src/__tests__/messages.test.ts
git commit -m "feat(tui): a resumed transcript rebuilds its tool rows as episodes"
```

Expected: 0 failures, `type-check` clean, `lint` 0 errors. Other suites may fail here if they asserted the flat trail shape -- fix those assertions in this commit and say in the message that they encoded the old shape.

---

### Task 3: Pin fold-id parity

The property the whole change rests on: a resumed transcript folds on the same keys a live one does. Without this test, either side can drift and nothing notices until a reader's fold stops working.

**Files:**
- Test: `ui-tui/src/__tests__/messages.test.ts`

**Interfaces:**
- Consumes: `toTranscriptMessages` (Task 2), `turnController` live path

- [ ] **Step 1: Write the test**

Append to `ui-tui/src/__tests__/messages.test.ts`:

```ts
describe('fold-id parity between live and resumed transcripts', () => {
  it('mints the same fold ids for the same calls', () => {
    // `foldStore` keys on `seg:<firstToolCallId>` and `call:<toolCallId>` -- the
    // transport's own ids. A resumed transcript that minted anything else would
    // render identically and still lose every fold the reader opened.
    turnController.reset()
    turnController.recordEpisodeStart(0)
    turnController.recordToolStart('call-1', 'read_file', 'a.ts')
    turnController.recordToolComplete('call-1', 'read_file', undefined, 'contents', 1.2)

    const { finalMessages } = turnController.recordMessageComplete({ text: 'here it is' })
    const live = finalMessages.find(m => m.kind === 'episodes')!

    const resumed = toTranscriptMessages([
      {
        role: 'assistant',
        text: '',
        tool_calls: [{ arguments: '{"path":"a.ts"}', id: 'call-1', name: 'read_file' }]
      },
      { duration_ms: 1200, name: 'read_file', role: 'tool', text: 'contents', tool_call_id: 'call-1' },
      { role: 'assistant', text: 'here it is' }
    ]).find(m => m.kind === 'episodes')!

    expect(resumed.episodes!.map(ep => ep.tools.map(tool => tool.id))).toEqual(
      live.episodes!.map(ep => ep.tools.map(tool => tool.id))
    )
    expect(resumed.foldId).toBe(live.foldId)
  })
})
```

Add `import { turnController } from '../app/turnController.js'` at the top of the file if it is not already imported.

- [ ] **Step 2: Run it**

Run: `cd ui-tui && npx vitest run src/__tests__/messages.test.ts --no-file-parallelism`
Expected: PASS. If `foldId` differs, Task 2's `foldSeed` is not the first call's id — fix Task 2, not this test.

- [ ] **Step 3: Prove it is load-bearing**

Temporarily change Task 2's `foldSeed: calls[0]!.id` to `foldSeed: `x${calls[0]!.id}``, re-run, and confirm **only** this test goes red. Restore the line.

- [ ] **Step 4: Commit**

```bash
git add ui-tui/src/__tests__/messages.test.ts
git commit -m "test(tui): pin the fold ids a resumed transcript mints to the live ones"
```

---

### Task 4: The backend derives `dag_run_id`

**Files:**
- Modify: `raven/rpc/methods/session.py:262-284` (the `extra_key` loop), `raven/rpc/models.py:1970` (`TranscriptMessage`)
- Regenerate: `rpc-schema/openrpc.json`, `ui-tui/src/rpc/generated.ts`, `ui/src/rpc/generated.ts`
- Test: `tests/test_rpc_session.py`

**Interfaces:**
- Produces: `TranscriptMessage.dag_run_id: str | None` — the run a `run_subagent_dag` call started, derived from the stored result text

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rpc_session.py`:

```python
def test_a_dag_tool_row_names_the_run_it_started():
    """The run id is in the result text the tool authored; the client should not
    have to parse prose to find it."""
    rows = _wire_messages(
        [
            {
                "role": "tool",
                "name": "run_subagent_dag",
                "tool_call_id": "call-1",
                "content": (
                    "DAG run 20260823T063516789587Z-1e97d546 finished: 2 completed, 0 failed "
                    "(of 2).\nRun dir: /root/.raven/x/subagents/mas_dag/20260823T063516789587Z-1e97d546"
                ),
            }
        ],
        "sess",
    )

    assert rows[0]["dag_run_id"] == "20260823T063516789587Z-1e97d546"


def test_a_non_dag_tool_row_names_no_run():
    rows = _wire_messages(
        [{"role": "tool", "name": "read_file", "tool_call_id": "c", "content": "contents"}],
        "sess",
    )

    assert "dag_run_id" not in rows[0]


def test_a_dag_row_whose_text_names_no_run_is_left_alone():
    """A graph rejected by validation returns an error, not a run."""
    rows = _wire_messages(
        [{"role": "tool", "name": "run_subagent_dag", "tool_call_id": "c", "content": "rejected: bad spec"}],
        "sess",
    )

    assert "dag_run_id" not in rows[0]
```

Import the helper at the top of the file with the other imports, matching whatever name `session.py` gives the wire-shaping function (`_wire_messages` in this plan — read the file and use its real name).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rpc_session.py -k dag_run -x -q`
Expected: FAIL — `KeyError: 'dag_run_id'`

- [ ] **Step 3: Derive the id**

In `raven/rpc/methods/session.py`, above the wire-shaping function:

```python
# "DAG run <id> finished: ..." -- the first line run_subagent_dag writes into its
# own result. Derived here rather than stored, so every session already on disk
# gains the link, and read here rather than in the client, which would be parsing
# a sentence it does not author.
_DAG_RUN_ID_RE = re.compile(r"\bDAG run (\d{8}T\d+Z-[0-9a-f]+)")
```

Inside the per-message loop, after the `extra_key` loop:

```python
        if m.get("name") == "run_subagent_dag" and isinstance(content, str):
            if match := _DAG_RUN_ID_RE.search(content):
                entry["dag_run_id"] = match.group(1)
```

Add `import re` to the imports if absent.

- [ ] **Step 4: Declare the field**

In `raven/rpc/models.py`, add to `TranscriptMessage` (after `tool_call_id`):

```python
    dag_run_id: str | None = Field(
        default=None,
        description=(
            "The run a run_subagent_dag call started, so a resumed transcript can fetch its graph "
            "through dag.get. Absent on every other tool, and on a graph that was rejected before "
            "it ran."
        ),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_rpc_session.py -k dag_run -x -q`
Expected: PASS, 3 tests.

- [ ] **Step 6: Regenerate the schema and both clients**

```bash
make rpc-schema || npm run gen:rpc --prefix ui
cd ui-tui && npm run type-check && cd ..
cd ui && npm run type-check && cd ..
```

Read `Makefile` for the real target name before running. Both clients' type-checkers must run: this repo has had a widened RPC type pass the schema-sync gate and still break a hand-written exhaustive `Record` two hops downstream, caught only by `tsc`.

- [ ] **Step 7: Run the python suite and commit**

```bash
uv run pytest -q
git add raven/rpc/methods/session.py raven/rpc/models.py rpc-schema/openrpc.json ui-tui/src/rpc/generated.ts ui/src/rpc/generated.ts tests/test_rpc_session.py
git commit -m "feat(rpc): a resumed dag tool row names the run it started"
```

---

### Task 5: Hydrate the graph on resume

**Files:**
- Modify: `ui-tui/src/app/useSessionLifecycle.ts:265-290` (the `session.resume` `.then`)
- Test: `ui-tui/src/__tests__/useSessionLifecycle.test.ts`

**Interfaces:**
- Consumes: `TranscriptMessage.dag_run_id` (Task 4), `toTranscriptMessages` (Task 2), existing `foldDagSnapshot` from `../domain/dagRun.js`
- Produces: `hydrateDagRuns(rows, msgs, rpc, sid): Promise<Msg[]>` in `ui-tui/src/domain/messages.ts` — attaches a `DagRunState` to each `EpisodeTool` whose call named a run

- [ ] **Step 1: Write the failing test**

Append to `ui-tui/src/__tests__/useSessionLifecycle.test.ts`:

```ts
describe('hydrateDagRuns', () => {
  const ROWS = [
    {
      role: 'assistant',
      text: '',
      tool_calls: [{ arguments: '{"nodes":[]}', id: 'call-1', name: 'run_subagent_dag' }]
    },
    { dag_run_id: 'dag-1', name: 'run_subagent_dag', role: 'tool', text: 'DAG run dag-1 finished', tool_call_id: 'call-1' }
  ]

  it('attaches the run its call started', async () => {
    const rpc = vi.fn(async () => ({
      run: {
        dir: '/runs/dag-1',
        files: [{ node: 'a', prompt_template: 'do a', status: 'completed', subagent: 'echo' }],
        finalized: true,
        run_id: 'dag-1',
        summary: { completed: 1, total: 1 }
      }
    }))

    const msgs = await hydrateDagRuns(ROWS, toTranscriptMessages(ROWS), rpc, 'sess')
    const tool = msgs.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!

    expect(tool.dag?.runId).toBe('dag-1')
    expect(tool.dag?.nodes[0]?.promptTemplate).toBe('do a')
  })

  it('leaves the row without a graph when the run dir is gone', async () => {
    // Deleting the outputs must cost the picture, not the transcript.
    const rpc = vi.fn(async () => {
      throw new Error('no such run')
    })

    const msgs = await hydrateDagRuns(ROWS, toTranscriptMessages(ROWS), rpc, 'sess')

    expect(msgs.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!.dag).toBeUndefined()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui-tui && npx vitest run src/__tests__/useSessionLifecycle.test.ts --no-file-parallelism`
Expected: FAIL — `hydrateDagRuns is not defined`

- [ ] **Step 3: Implement it**

Append to `ui-tui/src/domain/messages.ts`:

```ts
/**
 * Attach each resumed DAG call's graph, read back off disk.
 *
 * Done before the transcript is set rather than while rendering it, so drawing
 * stays a pure function of state: a fetch hung off the render would fire again
 * on every re-render and reorder against the reader's own clicks.
 *
 * A run whose directory is gone attaches nothing. The row then reads as it did
 * before graphs existed, which is the honest rendering of "the outputs were
 * deleted" -- an empty frame would claim the run had no nodes.
 */
export const hydrateDagRuns = async (
  rows: unknown,
  msgs: Msg[],
  rpc: <R>(method: string, params: Record<string, unknown>) => Promise<R>,
  sessionId: string
): Promise<Msg[]> => {
  if (!Array.isArray(rows)) {
    return msgs
  }

  const byCall = new Map<string, string>()

  for (const row of rows) {
    const { dag_run_id: runId, tool_call_id: callId } = (row ?? {}) as TranscriptRow

    if (runId && callId) {
      byCall.set(callId, runId)
    }
  }

  if (!byCall.size) {
    return msgs
  }

  const runs = new Map<string, DagRunState>()

  await Promise.all(
    [...new Set(byCall.values())].map(async runId => {
      try {
        const result = await rpc<{ run: DagRunSnapshot }>('dag.get', { run_id: runId, session_key: sessionId })

        runs.set(runId, foldDagSnapshot(null, result.run))
      } catch {
        // Reported nowhere on purpose: a missing run dir is an ordinary state
        // for an old session, not an error the reader has to acknowledge.
      }
    })
  )

  for (const msg of msgs) {
    for (const episode of msg.episodes ?? []) {
      for (const tool of episode.tools) {
        const run = runs.get(byCall.get(tool.id) ?? '')

        if (run) {
          tool.dag = run
        }
      }
    }
  }

  return msgs
}
```

Add to `TranscriptRow`: `dag_run_id?: string`. Add the imports:

```ts
import type { DagRunState } from './dagRun.js'
import type { DagRunSnapshot } from '../rpc/index.js'

import { foldDagSnapshot } from './dagRun.js'
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui-tui && npx vitest run src/__tests__/useSessionLifecycle.test.ts --no-file-parallelism`
Expected: PASS, 2 tests.

- [ ] **Step 5: Call it from resume**

In `ui-tui/src/app/useSessionLifecycle.ts`, replace:

```ts
              const resumed = toTranscriptMessages(r.messages)

              setHistoryItems(r.info ? [introMsg(r.info), ...resumed] : resumed)
```

with:

```ts
              const resumed = await hydrateDagRuns(r.messages, toTranscriptMessages(r.messages), rpc, r.session_id)

              setHistoryItems(r.info ? [introMsg(r.info), ...resumed] : resumed)
```

and make the enclosing `.then(raw => {` callback `async`. Import `hydrateDagRuns` alongside `toTranscriptMessages`.

- [ ] **Step 6: Run the suite and commit**

```bash
cd ui-tui && npx vitest run --no-file-parallelism && npm run type-check && npm run lint && cd ..
git add ui-tui/src/domain/messages.ts ui-tui/src/app/useSessionLifecycle.ts ui-tui/src/__tests__/useSessionLifecycle.test.ts
git commit -m "feat(tui): a resumed dag call gets its graph back"
```

---

### Task 6: A DAG stretch opens by default

**Files:**
- Modify: `ui-tui/src/app/foldStore.ts`, `ui-tui/src/components/episodeView.tsx:265-274` (`dagFor`), `:216-243` (`WorkSegment` props), `:488-502` (`renderWork`)
- Test: `ui-tui/src/__tests__/foldStore.test.tsx`, `ui-tui/src/__tests__/episodeView.test.tsx`

**Interfaces:**
- Produces: `isFoldOpen(scope: string, key: string, defaultOpen?: boolean): boolean`, `$folds: atom<Record<string, { closed: readonly string[]; open: readonly string[] }>>`

- [ ] **Step 1: Write the failing store test**

Append to `ui-tui/src/__tests__/foldStore.test.tsx`:

```tsx
describe('a content-driven default', () => {
  it('resolves an untouched fold to the default it was given', () => {
    resetFolds()

    expect(isFoldOpen('s', 'seg:1', true)).toBe(true)
    expect(isFoldOpen('s', 'seg:1', false)).toBe(false)
  })

  it('remembers a close against a default-open fold', () => {
    // The reason the store needs three states: with open ids alone, closing a
    // default-open stretch is indistinguishable from never having touched it,
    // so the next remount reopens what the reader just shut.
    resetFolds()
    toggleFold('s', 'seg:1', true)

    expect(isFoldOpen('s', 'seg:1', true)).toBe(false)
  })

  it('clears both sets', () => {
    toggleFold('s', 'seg:1', true)
    resetFolds()

    expect(isFoldOpen('s', 'seg:1', true)).toBe(true)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui-tui && npx vitest run src/__tests__/foldStore.test.tsx --no-file-parallelism`
Expected: FAIL — `isFoldOpen` takes two arguments and returns `false` for an untouched key.

- [ ] **Step 3: Make the store tri-state**

Replace the body of `ui-tui/src/app/foldStore.ts` below its header comment:

```ts
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui-tui && npx vitest run src/__tests__/foldStore.test.tsx --no-file-parallelism`
Expected: PASS. Other suites reading `$folds` as a string array will fail — fix their fixtures in this task.

- [ ] **Step 5: Write the failing view test**

`episodeView.test.tsx` already has `view(episodes, extra)`, `call(id, name, summary, extra)` and
`step(index, narration, tools)` helpers, and seeds folds through the `openKeys`
prop rather than the store -- use them. Append:

```tsx
const DAG_RUN = {
  done: true,
  nodes: [
    { dependsOn: [], id: 'a', promptTemplate: 'do a', status: 'completed' as const, subagent: 'echo' },
    { dependsOn: ['a'], id: 'b', promptTemplate: 'do b', status: 'completed' as const, subagent: 'echo' }
  ],
  runId: 'dag-1'
}

describe('a stretch holding a dag call', () => {
  it('opens itself, so the graph is drawn without a click', () => {
    const f = view([
      step(0, 'planning', [
        call('s1', 'read_skill', 'local/subagent-dag-orchestration'),
        call('s2', 'run_subagent_dag', '2 nodes: a, b', { dag: DAG_RUN })
      ])
    ])

    // The box corner: the graph rendered. Without the default this stretch
    // folds to one summary row and draws nothing.
    expect(f).toContain('\u256d')
    expect(f).toContain('run subagent dag')
  })

  it('keeps the graph when the reader folds it by hand', () => {
    // `openKeys` seeds the reader's decisions; a stretch that defaults open is
    // closed by naming it here, which is what the tri-state store records.
    const f = view(
      [
        step(0, 'planning', [
          call('s1', 'read_skill', 'local/subagent-dag-orchestration'),
          call('s2', 'run_subagent_dag', '2 nodes: a, b', { dag: DAG_RUN })
        ])
      ],
      { closedKeys: ['seg:s1'] }
    )

    expect(f).toContain('\u256d')
  })

  it('leaves a stretch without a dag call folded', () => {
    const f = view([
      step(0, 'looking', [call('t1', 'read_file', 'a.ts'), call('t2', 'read_file', 'b.ts')])
    ])

    expect(f).not.toContain('\u256d')
  })
})
```

`EpisodeView` needs a `closedKeys?: readonly string[]` prop alongside the
existing `openKeys`, seeding the closed half the same way. Add it in step 6.

**Do not add a `dag` to the fixture used by the `RETIRED` test.** That test
asserts `│`, `├` and five other glyphs never appear, as a contract that the
transcript reads as a document rather than a control panel. The DAG graph draws
`│` and `├` legitimately, so the contract has to stay scoped to dag-free
episodes -- widening it would either break this feature or gut the contract.

- [ ] **Step 6: Wire the default and the folded graph**

First fix the seam the tri-state breaks. `episodeView.tsx:424` reads
`new Set([...(openKeys ?? []), ...(stored[scope] ?? [])])`, which spreads the
store's value as an array -- it is now an object and will not iterate. Replace
the memo and `toggle` with:

```tsx
  const stored = useStore($folds)
  const seeded = useMemo(
    () => ({
      closed: new Set(closedKeys ?? []),
      open: new Set([...(openKeys ?? []), ...(stored[scope]?.open ?? [])])
    }),
    [closedKeys, openKeys, scope, stored]
  )
  const foldOpen = (key: string, defaultOpen: boolean) =>
    seeded.open.has(key) ? true : seeded.closed.has(key) || (stored[scope]?.closed ?? []).includes(key) ? false : defaultOpen
  const toggle = (key: string) => toggleFold(scope, key)
```

Add `closedKeys?: readonly string[]` to the props. Then:

```tsx
  const renderWork = (seg: Extract<Segment, { kind: 'work' }>) => {
    // The one tool whose result is a picture: a summary line cannot say which
    // node failed, so a stretch holding one opens itself, and folding it by hand
    // still leaves the graph.
    const hasDag = seg.tools.some(tool => tool.dag)

    return (
      <WorkSegment
        compact={compact}
        defaultOpen={hasDag}
        isOpen={foldOpen(`seg:${seg.key}`, hasDag)}
        key={`w:${seg.key}`}
        live={seg.live}
        now={now}
        openCalls={openCalls}
        t={t}
        toggleCall={id => toggle(`call:${id}`)}
        toggleSelf={() => toggleFold(scope, `seg:${seg.key}`, hasDag)}
        tools={seg.tools}
        width={width}
      />
    )
  }
```

Add `defaultOpen: boolean` to `WorkSegment`'s props and destructuring. In the folded (`!isOpen`, non-`solo`) branch, render the graph after the summary row:

```tsx
        {seg.tools.map(tool => dagFor(tool, INDENT + STEP))}
```

Rewrite the comment above `dagFor` (`episodeView.tsx:265-269`): the sentence "Under a summary row it does not: a folded stretch is one row, and a graph is not one row" is now false. Replace it with why the DAG call is the exception — its result *is* the picture, so a folded stretch that hides it hides the answer.

- [ ] **Step 7: Run the suite and commit**

```bash
cd ui-tui && npx vitest run --no-file-parallelism && npm run type-check && npm run lint && cd ..
git add ui-tui/src/app/foldStore.ts ui-tui/src/components/episodeView.tsx ui-tui/src/__tests__/foldStore.test.tsx ui-tui/src/__tests__/episodeView.test.tsx
git commit -m "feat(tui): a stretch holding a dag call opens itself and keeps its graph"
```

---

### Task 7: Build the bundle and verify in a real terminal

Tests do not prove what `raven tui` runs: it executes a prebuilt bundle, and nothing rebuilds it automatically.

**Files:** none committed (`ui-tui/dist/entry.js` is gitignored)

- [ ] **Step 1: Build**

```bash
npm run build --prefix ui-tui
grep -c "foldRowsIntoEpisodes\|hydrateDagRuns" ui-tui/dist/entry.js
```

Expected: a non-zero count. A zero means the bundle is stale and every claim below would be about old code.

- [ ] **Step 2: Drive it**

Start the TUI, run a DAG (`run_subagent_dag` with two trivial nodes), then `/resume` that session and confirm, in this order:

1. the stretch holding the DAG call is open without a click, and the graph is drawn;
2. folding the stretch by hand keeps the graph;
3. after `/resume`, the tool rows are individual rows, not one comma-joined line;
4. after `/resume`, the DAG graph is drawn;
5. after `/resume`, clicking a node row expands its prompt and output;
6. after `/resume`, clicking a node's box in the graph toggles that same block.

- [ ] **Step 3: Record the result**

Write what you observed for each of the six, including any that failed. Do not report the feature as working on the strength of the test suite alone.

---

## Self-Review

**Spec coverage.** Section A (reconstruct episodes) — Tasks 1, 2. Fold-id parity — Task 3. Section B (`dag_run_id`, hydration, node expansion) — Tasks 4, 5. Section C (tri-state fold, default open, graph under a folded summary) — Task 6. "Parity is measured against live" — encoded as a global constraint and as `ok: true` in Task 1's core with no `diff` anywhere. Files table — every row appears in a task except `ui/src/rpc/generated.ts`, which Task 4 step 6 regenerates. Risks (blast radius) — Task 2 step 6 and Task 6 step 4 both call out that neighbouring suites asserting the old shape must be fixed in the same commit.

**Placeholders.** None. Task 6 step 5 originally deferred its fixture to "the file's existing one"; that was a placeholder and has been replaced with literal code built on the file's real `view` / `call` / `step` helpers.

**Type consistency.** `FoldRow` / `FoldCall` / `foldRowsIntoEpisodes` are defined in Task 1 and used with the same names and fields in Tasks 2 and 5. `isFoldOpen(scope, key, defaultOpen)` is defined in Task 6 step 3 and called with three arguments in step 6. `hydrateDagRuns(rows, msgs, rpc, sessionId)` is defined in Task 5 step 3 and called with four arguments in step 5. `dag_run_id` is produced in Task 4 and read in Task 5.
