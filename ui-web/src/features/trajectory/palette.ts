/* One colour and one word per kind of entry, for the list's type tag today
 * and the duration bar later: both read the same table so a tool call is the
 * same purple in the row and in the bar.
 *
 * The kinds the index names are an open set (a span's own io slots, an
 * artifact's name, an evidence slot), so only the seven the conversation is
 * made of get a colour of their own; everything else is one stable grey and
 * keeps its own name on the row, which is more honest than a colour the reader
 * cannot look up.
 */

import { t } from '../../i18n/t'

/* The seven, each with the catalogue key for its tag. Literal keys rather than
   a key built from the kind, so the i18n gate can hold every one of them to
   the catalogue. */
const KNOWN: Record<string, string> = {
  'user.input': 'gui.trajectory.kind.user_input',
  'turn.end': 'gui.trajectory.kind.turn_end',
  'llm.input': 'gui.trajectory.kind.llm_input',
  'llm.thinking': 'gui.trajectory.kind.llm_thinking',
  'llm.output': 'gui.trajectory.kind.llm_output',
  'tool.input': 'gui.trajectory.kind.tool_input',
  'tool.output': 'gui.trajectory.kind.tool_output',
}

/* The kinds beyond the seven: named, but drawn in the neutral grey. The
   index spells a span's io pair as `<span>.input` / `<span>.output`, an
   outer-only step as `<span>.summary`, and a personalize step as
   `personalize.<step>.input` / `.output`; the suffix tables below give each
   family one word. */
const NAMED: Record<string, string> = {
  'skill.read': 'gui.trajectory.kind.skill_read',
  'skill.inject': 'gui.trajectory.kind.skill_inject',
  'skill.rewrite.input': 'gui.trajectory.kind.skill_rewrite_input',
  'skill.rewrite.output': 'gui.trajectory.kind.skill_rewrite_output',
  'skill.gate.input': 'gui.trajectory.kind.skill_gate_input',
  'skill.gate.output': 'gui.trajectory.kind.skill_gate_output',
  'context.curate.input': 'gui.trajectory.kind.context_curate_input',
  'context.curate.output': 'gui.trajectory.kind.context_curate_output',
  'memory.recall': 'gui.trajectory.kind.memory_recall',
  'memory.store': 'gui.trajectory.kind.memory_store',
  'memory.feedback.summary': 'gui.trajectory.kind.memory_feedback',
  'memory.enqueue.summary': 'gui.trajectory.kind.memory_enqueue',
  'memory.extract.summary': 'gui.trajectory.kind.memory_extract',
  'memory.profile_refresh.summary': 'gui.trajectory.kind.memory_profile_refresh',
  'memory.consolidate.summary': 'gui.trajectory.kind.memory_consolidate',
  'subagent.run': 'gui.trajectory.kind.subagent_run',
  'span.error': 'gui.trajectory.kind.span_error',
  'span.malformed': 'gui.trajectory.kind.span_malformed',
  'span.unreadable': 'gui.trajectory.kind.span_unreadable',
  'plugin.load.summary': 'gui.trajectory.kind.plugin_load',
}

const PREFIXED: Array<[prefix: string, suffix: string, key: string]> = [
  ['personalize.', '.input', 'gui.trajectory.kind.personalize_input'],
  ['personalize.', '.output', 'gui.trajectory.kind.personalize_output'],
  ['subagent.external', '', 'gui.trajectory.kind.subagent_external'],
]

/** The catalogue key for a kind's word, or null for a kind this page has no word for. */
export function kindKey(kind: string): string | null {
  const direct = KNOWN[kind] ?? NAMED[kind]
  if (direct !== undefined) return direct
  for (const [prefix, suffix, key] of PREFIXED) {
    if (kind.startsWith(prefix) && kind.endsWith(suffix)) return key
  }
  return kind.endsWith('.summary') ? 'gui.trajectory.kind.summary' : null
}

export const KNOWN_KINDS: readonly string[] = Object.keys(KNOWN)

/** Every kind with a word of its own, for the test that holds the catalogue to them. */
export const NAMED_KINDS: readonly string[] = [...Object.keys(KNOWN), ...Object.keys(NAMED)]

export const isKnownKind = (kind: string): boolean => kind in KNOWN

/* The class suffix is the kind with its dot folded, so `styles.css` can name
   `.trajectory-k-llm-input` and the duration bar can read the same variable
   name back (`--trajectory-c-llm-input-fg`). */
export const kindSlug = (kind: string): string => (isKnownKind(kind) ? kind.replace(/\./g, '-') : 'other')

export const kindClass = (kind: string): string => `trajectory-k-${kindSlug(kind)}`

/** The word on the tag: the catalogue's for a kind it names, the kind itself otherwise. */
export function kindLabel(kind: string): string {
  const key = kindKey(kind)
  return key === null ? kind : t(key)
}

/** Every word the tag column may need to fit, in the current language. */
export const kindLabels = (): string[] => {
  const keys = new Set<string>([...Object.values(KNOWN), ...Object.values(NAMED), ...PREFIXED.map((p) => p[2]), 'gui.trajectory.kind.summary'])
  return [...keys].map((key) => t(key))
}
