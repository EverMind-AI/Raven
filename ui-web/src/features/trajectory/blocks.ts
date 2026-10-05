/* The words for what a detail is made of: a title per block id, a sentence
 * per availability, reason, integrity, note, status and timing code, and the
 * two readings the overview makes of a block's preview.
 *
 * Every table is literal keys into the catalogue, so the i18n gate can hold
 * each one to an entry, and every lookup answers null for a code it does not
 * know rather than inventing a sentence: the backend's vocabulary may grow
 * before this file does, and a bare code on screen is more honest than a
 * wrong word. Pure functions, no DOM, no gateway.
 */

import type { Availability, TrajectoryBlockDescriptor } from './types'

const BLOCK_TITLES: Record<string, string> = {
  content: 'gui.trajectory.block.content',
  media: 'gui.trajectory.block.media',
  origin: 'gui.trajectory.block.origin',
  capabilities: 'gui.trajectory.block.capabilities',
  turn: 'gui.trajectory.block.turn',
  messages: 'gui.trajectory.block.messages',
  system: 'gui.trajectory.block.system',
  prompt: 'gui.trajectory.block.prompt',
  model: 'gui.trajectory.block.model',
  tools: 'gui.trajectory.block.tools',
  request: 'gui.trajectory.block.request',
  thinking: 'gui.trajectory.block.thinking',
  thinkingBlocks: 'gui.trajectory.block.thinkingBlocks',
  toolCalls: 'gui.trajectory.block.toolCalls',
  finish: 'gui.trajectory.block.finish',
  usage: 'gui.trajectory.block.usage',
  response: 'gui.trajectory.block.response',
  tool: 'gui.trajectory.block.tool',
  params: 'gui.trajectory.block.params',
  schema: 'gui.trajectory.block.schema',
  result: 'gui.trajectory.block.result',
  skill: 'gui.trajectory.block.skill',
  skills: 'gui.trajectory.block.skills',
  stats: 'gui.trajectory.block.stats',
  query: 'gui.trajectory.block.query',
  decision: 'gui.trajectory.block.decision',
  task: 'gui.trajectory.block.task',
  candidates: 'gui.trajectory.block.candidates',
  agent: 'gui.trajectory.block.agent',
  settings: 'gui.trajectory.block.settings',
  hits: 'gui.trajectory.block.hits',
  injected: 'gui.trajectory.block.injected',
  used: 'gui.trajectory.block.used',
  position: 'gui.trajectory.block.position',
  operation: 'gui.trajectory.block.operation',
  qa: 'gui.trajectory.block.qa',
  summary: 'gui.trajectory.block.summary',
  plugin: 'gui.trajectory.block.plugin',
  contribution: 'gui.trajectory.block.contribution',
  transcript: 'gui.trajectory.block.transcript',
  frames: 'gui.trajectory.block.frames',
  attributes: 'gui.trajectory.block.attributes',
  error: 'gui.trajectory.block.error',
  timing: 'gui.trajectory.block.timing',
  relations: 'gui.trajectory.block.relations',
  integrity: 'gui.trajectory.block.integrity',
  raw: 'gui.trajectory.block.raw',
}

/** Every block id the registry can name, for the test that holds the catalogue to them. */
export const BLOCK_IDS: readonly string[] = Object.keys(BLOCK_TITLES)

/** The catalogue key for a block's title, or null for an id this page has no word for. */
export const blockTitleKey = (id: string): string | null => BLOCK_TITLES[id] ?? null

const AVAILABILITY: Record<string, string> = {
  empty: 'gui.trajectory.avail.empty',
  not_recorded: 'gui.trajectory.avail.not_recorded',
  missing: 'gui.trajectory.avail.missing',
  truncated: 'gui.trajectory.avail.truncated',
  unreadable: 'gui.trajectory.avail.unreadable',
  unsupported: 'gui.trajectory.avail.unsupported',
}

/** The sentence for an availability other than `available`; null for that one and for a code unknown here. */
export const availabilityKey = (code: Availability | string): string | null => AVAILABILITY[code] ?? null

const REASON: Record<string, string> = {
  not_loaded: 'gui.trajectory.reason.not_loaded',
  schema_unproven: 'gui.trajectory.reason.schema_unproven',
  preview_dropped: 'gui.trajectory.reason.preview_dropped',
  not_recorded: 'gui.trajectory.reason.not_recorded',
  artifact_missing: 'gui.trajectory.reason.artifact_missing',
  artifact_unreadable: 'gui.trajectory.reason.artifact_unreadable',
  artifact_outside_store: 'gui.trajectory.reason.artifact_outside_store',
  artifact_truncated: 'gui.trajectory.reason.artifact_truncated',
}

export const reasonKey = (code: string): string | null => REASON[code] ?? null

const INTEGRITY: Record<string, string> = {
  artifact_outside_store: 'gui.trajectory.integrity.artifact_outside_store',
  artifact_missing: 'gui.trajectory.integrity.artifact_missing',
  artifact_unreadable: 'gui.trajectory.integrity.artifact_unreadable',
  artifact_truncated: 'gui.trajectory.integrity.artifact_truncated',
  blob_missing: 'gui.trajectory.integrity.blob_missing',
  blob_truncated: 'gui.trajectory.integrity.blob_truncated',
}

export const integrityKey = (code: string): string | null => INTEGRITY[code] ?? null

const NOTE: Record<string, string> = {
  outer_only: 'gui.trajectory.note.outer_only',
  in_progress_snapshot: 'gui.trajectory.note.in_progress_snapshot',
  data_incomplete: 'gui.trajectory.note.data_incomplete',
}

export const noteKey = (code: string): string | null => NOTE[code] ?? null

const STATUS: Record<string, string> = {
  running: 'gui.trajectory.status.running',
  ok: 'gui.trajectory.status.ok',
  error: 'gui.trajectory.status.error',
  cancelled: 'gui.trajectory.status.cancelled',
  unknown: 'gui.trajectory.status.unknown',
}

export const statusKey = (status: string): string | null => STATUS[status] ?? null

const EVIDENCE: Record<string, string> = {
  span_error: 'gui.trajectory.evidence.span_error',
  tool_error: 'gui.trajectory.evidence.tool_error',
  tool_result_error: 'gui.trajectory.evidence.tool_result_error',
  outer_only: 'gui.trajectory.evidence.outer_only',
}

export const evidenceKey = (code: string): string | null => EVIDENCE[code] ?? null

/* What the timing tab says about the charge: one sentence per basis, the
   same three the duration bar's hover will read. */
const BASIS: Record<string, string> = {
  zero: 'gui.trajectory.basis.zero',
  span_full: 'gui.trajectory.basis.span_full',
  shared: 'gui.trajectory.basis.shared',
  not_recorded: 'gui.trajectory.basis.not_recorded',
  unknown: 'gui.trajectory.basis.unknown',
}

export const basisKey = (basis: string): string | null => BASIS[basis] ?? null

const TIMING_LABELS: Record<string, string> = {
  event_time: 'gui.trajectory.timing.event_time',
  operation_start: 'gui.trajectory.timing.operation_start',
  operation_end: 'gui.trajectory.timing.operation_end',
  duration_ms: 'gui.trajectory.timing.duration',
  charged_ms: 'gui.trajectory.timing.charged',
  duration_owner: 'gui.trajectory.timing.owner',
}

/** The label for a timing row, or null for a key shown under its own name. */
export const timingLabelKey = (key: string): string | null => TIMING_LABELS[key] ?? null

/* What the overview shows of a block and whether there is more behind it. */
export interface PreviewSummary {
  /** The preview is a cut of the body, so a "see all" is warranted. */
  clipped: boolean
  /** How many items the body holds, when the descriptor says. */
  total: number | null
  /** How many items the preview shows, for the list-shaped renderers. */
  shown: number | null
}

/* The backend clips a text preview to six lines or six hundred characters and
   ends a cut one with an ellipsis; a JSON preview keeps six keys and says how
   many more there were; a list preview keeps three items and says the total.
   Reading those marks back is how the overview knows to offer the whole. */
export function previewSummary(block: TrajectoryBlockDescriptor): PreviewSummary {
  const total = typeof block.total_items === 'number' ? block.total_items : null
  if (block.reason === 'preview_dropped') return { clipped: true, total, shown: null }
  const preview = block.preview
  switch (block.renderer) {
    case 'text':
      return { clipped: typeof preview === 'string' && preview.endsWith('…'), total: null, shown: null }
    case 'json': {
      if (Array.isArray(preview)) return { clipped: total !== null && total > preview.length, total, shown: preview.length }
      const more = preview !== null && typeof preview === 'object' ? (preview as Record<string, unknown>).$more_keys : undefined
      return { clipped: typeof more === 'number' && more > 0, total: null, shown: null }
    }
    case 'messages':
    case 'items':
    case 'references': {
      const shown = Array.isArray(preview) ? preview.length : 0
      return { clipped: total !== null && total > shown, total, shown }
    }
    case 'key_values': {
      const shown = Array.isArray(preview) ? preview.length : 0
      return { clipped: total !== null && total > shown, total, shown }
    }
    default:
      return { clipped: false, total, shown: null }
  }
}
