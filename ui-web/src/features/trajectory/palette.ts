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
  'agent.reply': 'gui.trajectory.kind.agent_reply',
  'llm.input': 'gui.trajectory.kind.llm_input',
  'llm.thinking': 'gui.trajectory.kind.llm_thinking',
  'llm.output': 'gui.trajectory.kind.llm_output',
  'tool.input': 'gui.trajectory.kind.tool_input',
  'tool.output': 'gui.trajectory.kind.tool_output',
}

export const KNOWN_KINDS: readonly string[] = Object.keys(KNOWN)

export const isKnownKind = (kind: string): boolean => kind in KNOWN

/* The class suffix is the kind with its dot folded, so `styles.css` can name
   `.trajectory-k-llm-input` and the duration bar can read the same variable
   name back (`--trajectory-c-llm-input-fg`). */
export const kindSlug = (kind: string): string => (isKnownKind(kind) ? kind.replace(/\./g, '-') : 'other')

export const kindClass = (kind: string): string => `trajectory-k-${kindSlug(kind)}`

/** The word on the tag: the catalogue's for a known kind, the kind itself otherwise. */
export function kindLabel(kind: string): string {
  const key = KNOWN[kind]
  return key === undefined ? kind : t(key)
}
