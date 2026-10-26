/* The trajectory view's offline library: three recorded turns, the same for
 * every session, in the order the real index would sort them.
 *
 * The first turn is eight entries of the main session alone -- the user's
 * input, a model call expanded into input / thinking / output, a tool call
 * that failed, a second tool call that succeeded, and the agent's reply --
 * with the timing the duration bar reads: inputs charged zero, the model
 * output carrying its call, the failed tool carrying the failure mark. The
 * second turn dispatches three sub-agent runs, whose steps the index puts
 * among the turn's own rows: one that finished, one still running, and an
 * external agent that left a transcript; each run's entry opens its Sub-agent
 * Scope and each step names the scope it belongs to. The third turn is the
 * one the finished run's report started. The detail answers come from a
 * small table keyed by entry id, so a tab opened on the demo page shows a
 * body, not a blank.
 *
 * English content, deliberately: these are demo rows for a shape the real
 * gateway fills in; the page's own words come from the catalogue through t().
 *
 * The turn picker and the export answer from the same demo: three turns, the
 * last still running, and an export that is done the moment it is asked for.
 * There is no gateway behind the offline page, so its download link leads
 * nowhere; the wizard's own states are what the demo shows.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type {
  JsonValue,
  TrajectoryBlockDescriptor,
  TrajectoryBlockResult,
  TrajectoryDetailResult,
  TrajectoryEntry,
  TrajectoryExportStatusResult,
  TrajectoryIndexState,
  TrajectoryTurnsResult,
} from '../generated'

export interface TrajectoryFixture {
  fixtures: Fixtures
}

const EPOCH = 'demo-1'
const SEC = 1000

const READY: TrajectoryIndexState = {
  phase: 'ready',
  scanned_bytes: 4096,
  total_bytes: 4096,
  head_truncated: 0,
  recovering_traces: 0,
  unresolved_traces: 0,
  unresolved_dropped: 0,
  oversized_lines_dropped: 0,
  preview_pending: 0,
  failure: null,
}

interface Row {
  id: string
  kind: string
  span: string
  spanName: string
  slot: string
  parent: string | null
  startOffset: number
  endOffset: number
  phase: 0 | 1
  preview: string
  status: TrajectoryEntry['operation_status']
  evidence: string[]
  failure: boolean
  charged: number | null
  basis: TrajectoryEntry['timing_basis']
  owner: string
  meta: Record<string, JsonValue>
  /** The row's trace, turn and the turn's root span; the first turn's when left out. */
  trace?: string
  turn?: number
  turnSpan?: string
  turnStart?: boolean
  origin?: TrajectoryEntry['origin']
  /** The Sub-agent Scope the row belongs to, the one it opens, the one whose report started its turn. */
  scope?: string
  opens?: string
  by?: string
  /** Still going: no end and no duration yet. */
  running?: boolean
}

const RUN_A = 't2:runA'
const RUN_B = 't2:runB'
const RUN_X = 't2:runX'
const T2 = { trace: 't2', turn: 2, turnSpan: 'turn2' }
const T3 = { trace: 't3', turn: 3, turnSpan: 'turn3', origin: 'subagent' as const }

const ROWS: Row[] = [
  { id: 't:turn:turn.input', kind: 'user.input', span: 'turn', spanName: 'session.turn', slot: 'turn.input', parent: null, startOffset: 0, endOffset: 12, phase: 0, preview: 'Summarise the open issues in this repository.', status: 'ok', evidence: [], failure: false, charged: 0, basis: 'zero', owner: 't:turn:turn.output', meta: { tool_count: 2 }, turnStart: true },
  { id: 't:llm:llm.input', kind: 'llm.input', span: 'llm', spanName: 'llm.call', slot: 'llm.input', parent: 'turn', startOffset: 0.2, endOffset: 3.4, phase: 0, preview: 'Summarise the open issues in this repository.', status: 'ok', evidence: [], failure: false, charged: 0, basis: 'zero', owner: 't:llm:llm.output', meta: { model: 'demo-model', purpose: 'main', delta: 'first', new_from: 0, message_count: 2 } },
  { id: 't:llm:llm.thinking', kind: 'llm.thinking', span: 'llm', spanName: 'llm.call', slot: 'llm.thinking', parent: 'turn', startOffset: 0.2, endOffset: 3.4, phase: 1, preview: 'The user wants a summary; list the issues first, then read the two that look related.', status: 'ok', evidence: [], failure: false, charged: 0, basis: 'not_recorded', owner: 't:llm:llm.output', meta: { model: 'demo-model' } },
  { id: 't:llm:llm.output', kind: 'llm.output', span: 'llm', spanName: 'llm.call', slot: 'llm.output', parent: 'turn', startOffset: 0.2, endOffset: 3.4, phase: 1, preview: 'list_issues {"state": "open"} read_issue {"id": 42}', status: 'ok', evidence: [], failure: false, charged: 3200, basis: 'span_full', owner: 't:llm:llm.output', meta: { model: 'demo-model', input_tokens: 812, output_tokens: 96 } },
  { id: 't:tool1:tool.input', kind: 'tool.input', span: 'tool1', spanName: 'tool.call', slot: 'tool.input', parent: 'turn', startOffset: 3.5, endOffset: 4.1, phase: 0, preview: 'list_issues {"state": "open"}', status: 'error', evidence: ['tool_error'], failure: false, charged: 0, basis: 'zero', owner: 't:tool1:tool.output', meta: { tool: 'list_issues' } },
  { id: 't:tool1:tool.output', kind: 'tool.output', span: 'tool1', spanName: 'tool.call', slot: 'tool.output', parent: 'turn', startOffset: 3.5, endOffset: 4.1, phase: 1, preview: 'list_issues: Error: the issue tracker is unreachable (timeout after 600 ms)', status: 'error', evidence: ['tool_error'], failure: true, charged: 600, basis: 'span_full', owner: 't:tool1:tool.output', meta: { tool: 'list_issues' } },
  { id: 't:tool2:tool.output', kind: 'tool.output', span: 'tool2', spanName: 'tool.call', slot: 'tool.output', parent: 'turn', startOffset: 4.2, endOffset: 5.0, phase: 1, preview: 'read_issue: # Issue 42: flaky retry on the sync path ...', status: 'ok', evidence: [], failure: false, charged: 800, basis: 'span_full', owner: 't:tool2:tool.output', meta: { tool: 'read_issue' } },
  { id: 't:turn:turn.output', kind: 'turn.end', span: 'turn', spanName: 'session.turn', slot: 'turn.output', parent: null, startOffset: 0, endOffset: 12, phase: 1, preview: 'Two open issues; the tracker call timed out, so this is from the one issue I could read.', status: 'ok', evidence: [], failure: false, charged: 12000, basis: 'span_full', owner: 't:turn:turn.output', meta: { tool_count: 2 } },
  { ...T2, id: 't2:turn2:turn.input', kind: 'user.input', span: 'turn2', spanName: 'session.turn', slot: 'turn.input', parent: null, startOffset: 20, endOffset: 50, phase: 0, preview: 'Fix the flaky retry on issue 42 and update the docs.', status: 'ok', evidence: [], failure: false, charged: 0, basis: 'zero', owner: 't2:turn2:turn.output', meta: { tool_count: 3 }, turnStart: true },
  { ...T2, id: 't2:llm:llm.output', kind: 'llm.output', span: 'llm', spanName: 'llm.call', slot: 'llm.output', parent: 'turn2', startOffset: 20.2, endOffset: 21.5, phase: 1, preview: 'spawn coder: fix the retry; spawn writer: update the docs; ask claude-code to review', status: 'ok', evidence: [], failure: false, charged: 1300, basis: 'span_full', owner: 't2:llm:llm.output', meta: { model: 'demo-model', input_tokens: 940, output_tokens: 120 } },
  { ...T2, id: 't2:runA:subagent.run', kind: 'subagent.run', span: 'runA', spanName: 'subagent.run', slot: 'subagent.run', parent: 'turn2', startOffset: 22, endOffset: 48, phase: 0, preview: 'Fix the flaky retry on the sync path', status: 'ok', evidence: [], failure: false, charged: 26000, basis: 'span_full', owner: 't2:runA:subagent.run', meta: { label: 'Fix the flaky retry', task_id: 'task-a', agent: 'coder', instance: 'coder-1', handle: 'coder-1', dispatch: 'spawn', outcome: 'completed' }, opens: RUN_A },
  { ...T2, id: 't2:runB:subagent.run', kind: 'subagent.run', span: 'runB', spanName: 'subagent.run', slot: 'subagent.run', parent: 'turn2', startOffset: 22.2, endOffset: 22.2, phase: 0, preview: 'Update the docs for the retry change', status: 'running', evidence: ['subagent_in_progress'], failure: false, charged: null, basis: 'unknown', owner: 't2:runB:subagent.run', meta: { label: 'Update the retry docs', task_id: 'task-b', agent: 'writer', handle: 'task-b', dispatch: 'spawn' }, opens: RUN_B, running: true },
  { ...T2, id: 't2:runX:subagent.run', kind: 'subagent.run', span: 'runX', spanName: 'subagent.run', slot: 'subagent.run', parent: 'turn2', startOffset: 22.4, endOffset: 40, phase: 0, preview: 'Review the retry change', status: 'ok', evidence: [], failure: false, charged: 17600, basis: 'span_full', owner: 't2:runX:subagent.run', meta: { label: 'Review the retry change', task_id: 'task-x', agent: 'claude-code', handle: 'task-x', dispatch: 'spawn', outcome: 'completed' }, opens: RUN_X },
  { ...T2, id: 't2:b1:llm.output', kind: 'llm.output', span: 'b1', spanName: 'llm.call', slot: 'llm.output', parent: 'runB', startOffset: 23, endOffset: 27, phase: 1, preview: 'Drafting the docs change for the retry.', status: 'ok', evidence: [], failure: false, charged: 4000, basis: 'span_full', owner: 't2:b1:llm.output', meta: { model: 'demo-model' }, scope: RUN_B },
  { ...T2, id: 't2:a1:llm.output', kind: 'llm.output', span: 'a1', spanName: 'llm.call', slot: 'llm.output', parent: 'runA', startOffset: 23, endOffset: 30, phase: 1, preview: 'The retry has no backoff; add one with jitter.', status: 'ok', evidence: [], failure: false, charged: 7000, basis: 'span_full', owner: 't2:a1:llm.output', meta: { model: 'demo-model' }, scope: RUN_A },
  { ...T2, id: 't2:xext:artifact:subagent.external.transcript', kind: 'subagent.external.transcript', span: 'xext', spanName: 'subagent.external', slot: 'artifact:subagent.external.transcript', parent: 'runX', startOffset: 23, endOffset: 39, phase: 1, preview: 'claude-code: the retry change looks right; one test is missing.', status: 'ok', evidence: [], failure: false, charged: 16000, basis: 'span_full', owner: 't2:xext:artifact:subagent.external.transcript', meta: {}, scope: RUN_X },
  { ...T2, id: 't2:a2:tool.output', kind: 'tool.output', span: 'a2', spanName: 'tool.call', slot: 'tool.output', parent: 'runA', startOffset: 31, endOffset: 47, phase: 1, preview: 'edit_file: raven/sync.py patched with a jittered backoff', status: 'ok', evidence: [], failure: false, charged: 16000, basis: 'span_full', owner: 't2:a2:tool.output', meta: { tool: 'edit_file' }, scope: RUN_A },
  { ...T2, id: 't2:turn2:turn.output', kind: 'turn.end', span: 'turn2', spanName: 'session.turn', slot: 'turn.output', parent: null, startOffset: 20, endOffset: 50, phase: 1, preview: 'Three helpers are on it: a fix, the docs and a review.', status: 'ok', evidence: [], failure: false, charged: 30000, basis: 'span_full', owner: 't2:turn2:turn.output', meta: { tool_count: 3 } },
  { ...T3, id: 't3:turn3:turn.input', kind: 'user.input', span: 'turn3', spanName: 'session.turn', slot: 'turn.input', parent: null, startOffset: 60, endOffset: 66, phase: 0, preview: 'Report from the coder: the retry fix is in.', status: 'ok', evidence: [], failure: false, charged: 0, basis: 'zero', owner: 't3:turn3:turn.output', meta: { tool_count: 0 }, turnStart: true, by: RUN_A },
  { ...T3, id: 't3:llm:llm.output', kind: 'llm.output', span: 'llm', spanName: 'llm.call', slot: 'llm.output', parent: 'turn3', startOffset: 61, endOffset: 64, phase: 1, preview: 'The coder fixed the retry; the docs are still being written.', status: 'ok', evidence: [], failure: false, charged: 3000, basis: 'span_full', owner: 't3:llm:llm.output', meta: { model: 'demo-model' } },
  { ...T3, id: 't3:turn3:turn.output', kind: 'turn.end', span: 'turn3', spanName: 'session.turn', slot: 'turn.output', parent: null, startOffset: 60, endOffset: 66, phase: 1, preview: 'The retry fix is in; the docs are on their way.', status: 'ok', evidence: [], failure: false, charged: 6000, basis: 'span_full', owner: 't3:turn3:turn.output', meta: { tool_count: 0 }, by: RUN_A },
]

function iso(ms: number): string {
  return new Date(ms).toISOString()
}

function entry(row: Row, base: number, revision: number): TrajectoryEntry {
  const start = iso(base + row.startOffset * SEC)
  const end = iso(base + row.endOffset * SEC)
  const eventTime = row.phase === 0 ? start : end
  const trace = row.trace ?? 't'
  return {
    entry_id: row.id,
    revision,
    kind: row.kind,
    span_name: row.spanName,
    slot: row.slot,
    trace_id: trace,
    span_id: row.span,
    parent_span_id: row.parent,
    turn_span_id: row.turnSpan ?? 'turn',
    turn_number: row.turn ?? 1,
    turn_start: row.turnStart ?? false,
    origin: row.origin ?? 'main',
    subagent_scope: row.scope ?? null,
    opens_subagent_scope: row.opens ?? null,
    dispatched_by_subagent_scope: row.by ?? null,
    sort_key: [eventTime, row.phase, row.parent ? (row.phase === 0 ? 1 : -1) : 0, start, trace, row.span, 0],
    event_time: eventTime,
    preview: row.preview,
    operation_status: row.status,
    status_evidence: row.evidence,
    failure_entry: row.failure,
    integrity: [],
    operation_start: start,
    operation_end: row.running ? null : end,
    duration_ms: row.running ? null : Math.round((row.endOffset - row.startOffset) * SEC),
    charged_ms: row.charged,
    timing_basis: row.basis,
    duration_owner: row.owner,
    meta: row.meta,
  }
}

const TAIL: TrajectoryBlockDescriptor[] = [
  { id: 'timing', renderer: 'key_values', availability: 'available', preview: null, total_items: 7, related_operation: null, reason: null },
  { id: 'relations', renderer: 'key_values', availability: 'available', preview: null, total_items: 11, related_operation: null, reason: null },
  { id: 'raw', renderer: 'json', availability: 'available', preview: null, total_items: null, related_operation: null, reason: null },
]

const block = (
  id: string,
  renderer: TrajectoryBlockDescriptor['renderer'],
  preview: JsonValue,
  extra: Partial<TrajectoryBlockDescriptor> = {},
): TrajectoryBlockDescriptor => ({
  id, renderer, availability: 'available', preview, total_items: null, related_operation: null, reason: null, ...extra,
})

const SCHEMA_UNPROVEN: TrajectoryBlockDescriptor = {
  id: 'schema', renderer: 'json', availability: 'not_recorded', preview: null, total_items: null, related_operation: 'llm.input', reason: 'schema_unproven',
}

/* A sub-agent run's two blocks: the task it was given, and who ran it. */
function subagentBlocks(task: string, label: string, agent: string, instance: string | null): TrajectoryBlockDescriptor[] {
  const who: Array<{ key: string; value: JsonValue }> = [{ key: 'subagent.label', value: label }, { key: 'subagent.agent', value: agent }]
  if (instance) who.push({ key: 'subagent.instance', value: instance })
  return [block('task', 'text', task), block('agent', 'key_values', who, { total_items: who.length })]
}

/* One entry's own blocks, and the bodies that say more than the descriptor's
   preview does. Keyed by entry id rather than by kind: the two tool results
   on the list are different calls with different parameters and outcomes,
   and a tab opened on the successful one must not show the failed one's
   error. */
interface Own {
  blocks: TrajectoryBlockDescriptor[]
  bodies?: Record<string, JsonValue>
}

const ASK = 'Summarise the open issues in this repository.'
const REPLY = 'Two open issues; the tracker call timed out, so this is from the one issue I could read.'
const TRACKER_ERROR = 'Error: the issue tracker is unreachable (timeout after 600 ms)'
const ISSUE_42 = [
  '# Issue 42: flaky retry on the sync path',
  '',
  'The sync path retries a failed upload three times with no backoff, so a slow',
  'mirror sees three requests inside one second and rejects the last two.',
  'Linked from issue 40, which reports the same symptom from the other side.',
].join('\n')

const OWN: Record<string, Own> = {
  't:turn:turn.input': {
    blocks: [
      block('content', 'text', ASK),
      block('origin', 'key_values', [{ key: 'channel', value: 'gui' }], { total_items: 1 }),
    ],
  },
  't:llm:llm.input': {
    blocks: [
      block('messages', 'messages', [{ role: 'system', content: 'You are a careful assistant.' }, { role: 'user', content: ASK }], { total_items: 2 }),
      block('model', 'key_values', [{ key: 'model', value: 'demo-model' }], { total_items: 1 }),
      block('tools', 'items', ['list_issues', 'read_issue'], { total_items: 2 }),
      block('outline', 'items', null, { total_items: 2 }),
    ],
    bodies: {
      outline: {
        items: [
          { index: 0, role: 'system', bytes: 52, chars: 28, preview: 'You are a careful assistant.', partial: false, missing: false, cursor: 'outline-0' },
          { index: 1, role: 'user', bytes: 60, chars: ASK.length, preview: ASK, partial: false, missing: false, cursor: 'outline-1' },
        ],
        offset: 0,
      },
    },
  },
  't:llm:llm.thinking': {
    blocks: [
      block('thinking', 'text', 'The user wants a summary; list the issues first, then read the two that look related.', { related_operation: 'llm.output' }),
    ],
  },
  't:llm:llm.output': {
    blocks: [
      block('content', 'text', ''),
      block('toolCalls', 'items', ['list_issues#1', 'read_issue#2'], { total_items: 2 }),
      block('finish', 'key_values', [{ key: 'finish_reason', value: 'tool_calls' }], { total_items: 1 }),
      block('usage', 'key_values', [{ key: 'input_tokens', value: 812 }, { key: 'output_tokens', value: 96 }], { total_items: 2 }),
    ],
  },
  't:tool1:tool.input': {
    blocks: [
      block('params', 'json', { state: 'open' }),
      SCHEMA_UNPROVEN,
    ],
  },
  't:tool1:tool.output': {
    blocks: [
      block('result', 'text', TRACKER_ERROR),
      block('params', 'json', { state: 'open' }, { related_operation: 'tool.input' }),
      SCHEMA_UNPROVEN,
    ],
  },
  't:tool2:tool.output': {
    blocks: [
      block('result', 'text', '# Issue 42: flaky retry on the sync path ...'),
      block('params', 'json', { id: 42 }, { related_operation: 'tool.input' }),
      SCHEMA_UNPROVEN,
    ],
    bodies: { result: { text: ISSUE_42 } },
  },
  't:turn:turn.output': {
    blocks: [
      block('content', 'text', REPLY),
      block('capabilities', 'key_values', [{ key: 'turn.tool_count', value: 2 }], { total_items: 1 }),
    ],
  },
  't2:turn2:turn.input': { blocks: [block('content', 'text', 'Fix the flaky retry on issue 42 and update the docs.')] },
  't2:llm:llm.output': {
    blocks: [
      block('content', 'text', ''),
      block('toolCalls', 'items', ['spawn#1', 'spawn#2', 'spawn#3'], { total_items: 3 }),
      block('usage', 'key_values', [{ key: 'input_tokens', value: 940 }, { key: 'output_tokens', value: 120 }], { total_items: 2 }),
    ],
  },
  't2:runA:subagent.run': { blocks: subagentBlocks('Fix the flaky retry on the sync path: add a jittered backoff and a test.', 'Fix the flaky retry', 'coder', 'coder-1') },
  't2:runB:subagent.run': { blocks: subagentBlocks('Update the docs for the retry change.', 'Update the retry docs', 'writer', null) },
  't2:runX:subagent.run': { blocks: subagentBlocks('Review the retry change and say what is missing.', 'Review the retry change', 'claude-code', null) },
  't2:b1:llm.output': { blocks: [block('content', 'text', 'Drafting the docs change for the retry.')] },
  't2:a1:llm.output': { blocks: [block('content', 'text', 'The retry has no backoff; add one with jitter.')] },
  't2:xext:artifact:subagent.external.transcript': {
    blocks: [
      block('agent', 'key_values', [{ key: 'subagent.external.agent', value: 'claude-code' }, { key: 'subagent.external.transport', value: 'cli' }], { total_items: 2 }),
      block('result', 'key_values', [{ key: 'subagent.external.answer_chars', value: 64 }, { key: 'subagent.external.exit_code', value: 0 }], { total_items: 2 }),
      block('transcript', 'text', 'claude-code: the retry change looks right; one test is missing.'),
    ],
  },
  't2:a2:tool.output': {
    blocks: [
      block('result', 'text', 'raven/sync.py patched with a jittered backoff'),
      block('params', 'json', { path: 'raven/sync.py' }, { related_operation: 'tool.input' }),
    ],
  },
  't2:turn2:turn.output': { blocks: [block('content', 'text', 'Three helpers are on it: a fix, the docs and a review.')] },
  't3:turn3:turn.input': { blocks: [block('content', 'text', 'Report from the coder: the retry fix is in.')] },
  't3:llm:llm.output': { blocks: [block('content', 'text', 'The coder fixed the retry; the docs are still being written.')] },
  't3:turn3:turn.output': { blocks: [block('content', 'text', 'The retry fix is in; the docs are on their way.')] },
}

function blocksFor(row: Row): TrajectoryBlockDescriptor[] {
  const own = OWN[row.id]?.blocks ?? []
  const error: TrajectoryBlockDescriptor[] =
    row.status === 'error'
      ? [{ id: 'error', renderer: 'key_values', availability: 'available', preview: [{ key: 'evidence', value: row.evidence }], total_items: 2, related_operation: null, reason: null }]
      : []
  return [...own, ...error, ...TAIL]
}

function body(row: Row, descriptor: TrajectoryBlockDescriptor, base: number): JsonValue {
  const own = OWN[row.id]?.bodies?.[descriptor.id]
  if (own !== undefined) return own
  switch (descriptor.renderer) {
    case 'text':
      return { text: typeof descriptor.preview === 'string' ? descriptor.preview : row.preview }
    case 'json':
      return { value: descriptor.id === 'raw' ? { status: { code: row.status === 'error' ? 'ERROR' : 'OK' }, attributes: row.meta } : descriptor.preview }
    case 'messages':
      return { items: Array.isArray(descriptor.preview) ? descriptor.preview : [], offset: 0 }
    case 'items':
      return { items: Array.isArray(descriptor.preview) ? descriptor.preview : [], offset: 0 }
    case 'key_values':
      if (descriptor.id === 'timing') {
        return {
          items: [
            { key: 'event_time', value: iso(base + (row.phase === 0 ? row.startOffset : row.endOffset) * SEC), source: 'derived' },
            { key: 'operation_start', value: iso(base + row.startOffset * SEC), source: 'derived' },
            { key: 'operation_end', value: iso(base + row.endOffset * SEC), source: 'derived' },
            { key: 'duration_ms', value: Math.round((row.endOffset - row.startOffset) * SEC), source: 'derived' },
            { key: 'charged_ms', value: row.charged, source: 'derived' },
            { key: 'timing_basis', value: row.basis, source: 'derived' },
            { key: 'duration_owner', value: row.owner, source: 'derived' },
          ],
        }
      }
      if (descriptor.id === 'relations') {
        return {
          items: [
            { key: 'session_key', value: 'gui:demo', source: 'attribute' },
            { key: 'turn_number', value: row.turn ?? 1, source: 'derived' },
            { key: 'turn_span_id', value: row.turnSpan ?? 'turn', source: 'derived' },
            { key: 'trace_id', value: row.trace ?? 't', source: 'derived' },
            { key: 'span_id', value: row.span, source: 'derived' },
            { key: 'parent_span_id', value: row.parent, source: 'derived' },
            { key: 'origin', value: row.origin ?? 'main', source: 'derived' },
            { key: 'subagent_scope', value: row.scope ?? null, source: 'derived' },
            { key: 'opens_subagent_scope', value: row.opens ?? null, source: 'derived' },
            { key: 'dispatched_by_subagent_scope', value: row.by ?? null, source: 'derived' },
            {
              key: 'sibling_entries',
              value: ROWS.filter((r) => (r.trace ?? 't') === (row.trace ?? 't') && r.span === row.span && r.id !== row.id).map((r) => r.id),
              source: 'derived',
            },
          ],
        }
      }
      return {
        items: Array.isArray(descriptor.preview)
          ? (descriptor.preview as Array<{ key: string; value: JsonValue }>).map((item) => ({ ...item, source: 'artifact' }))
          : [],
      }
    default:
      return null
  }
}

export function createTrajectory(env: FixtureEnv): TrajectoryFixture {
  /* The recorded turn ended ten minutes before the library was built, read
     off the env's clock once: a finished entry keeps its revision, so its
     times must not move between a list and the timing tab opened a minute
     later. Two libraries born on the same instant still agree byte for byte. */
  const base = env.now() - 10 * 60 * SEC
  const entries = () => ROWS.map((row, index) => entry(row, base, index + 1))
  /* Total on purpose: a gate probes every responder with whatever params it
     has, and a demo page answering an unknown id with the first row is more
     useful than one that throws. */
  const rowOf = (id: string | undefined) => ROWS.find((row) => row.id === id) ?? ROWS[0]!

  return {
    fixtures: {
      'trajectory.state': () => ({ enabled: true, policy_revision: 1, recording_enabled: true }),
      'trajectory.list': () => ({
        epoch: EPOCH,
        snapshot_revision: ROWS.length,
        entries: entries(),
        next_cursor: null,
        index_state: READY,
        complete: true,
      }),
      'trajectory.changes': (p) => ({
        epoch: EPOCH,
        from_revision: p.after_revision ?? 0,
        to_revision: p.after_revision ?? 0,
        upserts: [],
        removed: [],
        has_more: false,
        reset_required: p.epoch !== undefined && p.epoch !== EPOCH,
        index_state: READY,
      }),
      'trajectory.detail': (p): TrajectoryDetailResult => {
        const row = rowOf(p.entry_id)
        const revision = ROWS.indexOf(row) + 1
        return {
          session_key: p.session_key ?? 'gui:demo',
          epoch: EPOCH,
          entry_id: row.id,
          entry_revision: revision,
          kind: row.kind,
          span_name: row.spanName,
          slot: row.slot,
          operation_status: row.status,
          status_evidence: row.evidence,
          failure_entry: row.failure,
          integrity: [],
          notes: [],
          blocks: blocksFor(row),
          revision_changed: p.entry_revision != null && p.entry_revision !== revision,
          truncated: false,
        }
      },
      'trajectory.block': (p): TrajectoryBlockResult => {
        const row = rowOf(p.entry_id)
        const blocks = blocksFor(row)
        const descriptor = blocks.find((b) => b.id === p.block_id) ?? blocks[0]!
        return {
          entry_id: row.id,
          entry_revision: p.entry_revision ?? ROWS.indexOf(row) + 1,
          epoch: p.epoch ?? EPOCH,
          block_id: descriptor.id,
          renderer: descriptor.renderer,
          availability: descriptor.availability,
          reason: descriptor.reason ?? null,
          data: descriptor.availability === 'available' ? body(row, descriptor, base) : null,
          next_cursor: null,
          total_items: descriptor.total_items ?? null,
          integrity: [],
          truncated: false,
        }
      },
      'trajectory.turns': (): TrajectoryTurnsResult => ({
        title: 'Plan a weekend in Kyoto',
        turns: TURNS.map((turn, index) => ({
          trace_id: turn.trace,
          number: index + 1,
          start: iso(base + turn.offset * SEC),
          input_preview: turn.input,
          in_progress: index === TURNS.length - 1,
        })),
      }),
      'trajectory.export': () => ({ job_id: 'exj-demo' }),
      'trajectory.export_status': (p): TrajectoryExportStatusResult => ({
        job_id: p.job_id ?? 'exj-demo',
        state: 'done',
        phase: 'done',
        file_name: 'Plan a weekend in Kyoto.tar.gz',
        download_url: '/trajectory/export/download?token=demo',
        size_bytes: 48213,
        counts: { spans: 42, artifacts: 17, messages: 9 },
        error: null,
      }),
      'trajectory.export_cancel': () => ({ ok: true }),
    },
  }
}

const TURNS = [
  { trace: 'trace-demo-1', offset: 0, input: 'Plan a weekend in Kyoto for two.' },
  { trace: 'trace-demo-2', offset: 120, input: 'Swap the second day for a day trip to Nara.' },
  { trace: 'trace-demo-3', offset: 300, input: 'Book the ryokan you suggested.' },
]
