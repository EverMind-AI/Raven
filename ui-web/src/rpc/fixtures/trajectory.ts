/* The trajectory view's offline library: one recorded turn, the same for
 * every session.
 *
 * Eight entries in the order the real index would sort them -- the user's
 * input, a model call expanded into input / thinking / output, a tool call
 * that failed, a second tool call that succeeded, and the agent's reply --
 * with the timing the duration bar reads: inputs charged zero, the model
 * output carrying its call, the failed tool carrying the failure mark. The
 * detail answers come from a small table keyed by entry id, so a tab opened
 * on the demo page shows a body, not a blank.
 *
 * English content, deliberately: these are demo rows for a shape the real
 * gateway fills in; the page's own words come from the catalogue through t().
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type {
  JsonValue,
  TrajectoryBlockDescriptor,
  TrajectoryBlockResult,
  TrajectoryDetailResult,
  TrajectoryEntry,
  TrajectoryIndexState,
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
}

const ROWS: Row[] = [
  { id: 't:turn:turn.input', kind: 'user.input', span: 'turn', spanName: 'session.turn', slot: 'turn.input', parent: null, startOffset: 0, endOffset: 12, phase: 0, preview: 'Summarise the open issues in this repository.', status: 'ok', evidence: [], failure: false, charged: 0, basis: 'zero', owner: 't:turn:turn.output', meta: { tool_count: 2 } },
  { id: 't:llm:llm.input', kind: 'llm.input', span: 'llm', spanName: 'llm.call', slot: 'llm.input', parent: 'turn', startOffset: 0.2, endOffset: 3.4, phase: 0, preview: 'Summarise the open issues in this repository.', status: 'ok', evidence: [], failure: false, charged: 0, basis: 'zero', owner: 't:llm:llm.output', meta: { model: 'demo-model', purpose: 'main', delta: 'first', new_from: 0, message_count: 2 } },
  { id: 't:llm:llm.thinking', kind: 'llm.thinking', span: 'llm', spanName: 'llm.call', slot: 'llm.thinking', parent: 'turn', startOffset: 0.2, endOffset: 3.4, phase: 1, preview: 'The user wants a summary; list the issues first, then read the two that look related.', status: 'ok', evidence: [], failure: false, charged: 0, basis: 'not_recorded', owner: 't:llm:llm.output', meta: { model: 'demo-model' } },
  { id: 't:llm:llm.output', kind: 'llm.output', span: 'llm', spanName: 'llm.call', slot: 'llm.output', parent: 'turn', startOffset: 0.2, endOffset: 3.4, phase: 1, preview: 'list_issues {"state": "open"} read_issue {"id": 42}', status: 'ok', evidence: [], failure: false, charged: 3200, basis: 'span_full', owner: 't:llm:llm.output', meta: { model: 'demo-model', input_tokens: 812, output_tokens: 96 } },
  { id: 't:tool1:tool.input', kind: 'tool.input', span: 'tool1', spanName: 'tool.call', slot: 'tool.input', parent: 'turn', startOffset: 3.5, endOffset: 4.1, phase: 0, preview: 'list_issues {"state": "open"}', status: 'error', evidence: ['tool_error'], failure: false, charged: 0, basis: 'zero', owner: 't:tool1:tool.output', meta: { tool: 'list_issues' } },
  { id: 't:tool1:tool.output', kind: 'tool.output', span: 'tool1', spanName: 'tool.call', slot: 'tool.output', parent: 'turn', startOffset: 3.5, endOffset: 4.1, phase: 1, preview: 'list_issues: Error: the issue tracker is unreachable (timeout after 600 ms)', status: 'error', evidence: ['tool_error'], failure: true, charged: 600, basis: 'span_full', owner: 't:tool1:tool.output', meta: { tool: 'list_issues' } },
  { id: 't:tool2:tool.output', kind: 'tool.output', span: 'tool2', spanName: 'tool.call', slot: 'tool.output', parent: 'turn', startOffset: 4.2, endOffset: 5.0, phase: 1, preview: 'read_issue: # Issue 42: flaky retry on the sync path ...', status: 'ok', evidence: [], failure: false, charged: 800, basis: 'span_full', owner: 't:tool2:tool.output', meta: { tool: 'read_issue' } },
  { id: 't:turn:turn.output', kind: 'turn.end', span: 'turn', spanName: 'session.turn', slot: 'turn.output', parent: null, startOffset: 0, endOffset: 12, phase: 1, preview: 'Two open issues; the tracker call timed out, so this is from the one issue I could read.', status: 'ok', evidence: [], failure: false, charged: 12000, basis: 'span_full', owner: 't:turn:turn.output', meta: { tool_count: 2 } },
]

function iso(ms: number): string {
  return new Date(ms).toISOString()
}

function entry(row: Row, base: number, revision: number): TrajectoryEntry {
  const start = iso(base + row.startOffset * SEC)
  const end = iso(base + row.endOffset * SEC)
  const eventTime = row.phase === 0 ? start : end
  return {
    entry_id: row.id,
    revision,
    kind: row.kind,
    span_name: row.spanName,
    slot: row.slot,
    trace_id: 't',
    span_id: row.span,
    parent_span_id: row.parent,
    turn_span_id: 'turn',
    turn_number: 1,
    turn_start: row.id === 't:turn:turn.input',
    origin: 'main',
    sort_key: [eventTime, row.phase, row.parent ? (row.phase === 0 ? 1 : -1) : 0, start, 't', row.span, 0],
    event_time: eventTime,
    preview: row.preview,
    operation_status: row.status,
    status_evidence: row.evidence,
    failure_entry: row.failure,
    integrity: [],
    operation_start: start,
    operation_end: end,
    duration_ms: Math.round((row.endOffset - row.startOffset) * SEC),
    charged_ms: row.charged,
    timing_basis: row.basis,
    duration_owner: row.owner,
    meta: row.meta,
  }
}

const TAIL: TrajectoryBlockDescriptor[] = [
  { id: 'timing', renderer: 'key_values', availability: 'available', preview: null, total_items: 7, related_operation: null, reason: null },
  { id: 'relations', renderer: 'key_values', availability: 'available', preview: null, total_items: 8, related_operation: null, reason: null },
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
            { key: 'turn_number', value: 1, source: 'derived' },
            { key: 'turn_span_id', value: 'turn', source: 'derived' },
            { key: 'trace_id', value: 't', source: 'derived' },
            { key: 'span_id', value: row.span, source: 'derived' },
            { key: 'parent_span_id', value: row.parent, source: 'derived' },
            { key: 'origin', value: 'main', source: 'derived' },
            { key: 'sibling_entries', value: ROWS.filter((r) => r.span === row.span && r.id !== row.id).map((r) => r.id), source: 'derived' },
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
    },
  }
}
