/* What the persona page reads and does.
 *
 * A Persona is a stored Harness carrying a coordinator seat: the durable
 * identity a conversation runs as, rather than a graph a turn dispatches. The
 * row is `playbooks.list`'s, narrowed to the fields the page draws, so a row
 * growing a field this page does not draw is not a change this page has to
 * make -- and a draft answers in the same shape, so one card draws both.
 */

export interface PersonaRow {
  name: string
  description: string
  artifact_kind: 'legacy' | 'workflow' | 'harness' | 'composite'
  coordinator: boolean
  /** What the main Raven is, in this Persona's words; absent on an older build. */
  coordinator_brief?: string
  workers: { label: string; agent: string }[]
  origin: string
  disabled: boolean
  error: string
}

/** One line of the making conversation, as the page draws it. */
export interface PersonaLine {
  role: 'user' | 'assistant'
  text: string
}

/** What the turn is doing right now, so the page is not a black box. */
export interface PersonaStep {
  kind: 'thinking' | 'tool' | 'saying' | 'done' | 'error'
  /** The tool's name, for a tool step; empty for the rest. */
  label: string
}

export interface PersonaSource {
  /** Every stored artifact; the page keeps the ones with a coordinator seat. */
  list(): Promise<PersonaRow[]>
  /** A conversation to describe a Persona in. Answers its session key. */
  openMaker(): Promise<string>
  /** Describe one. The turn generates a Persona and holds it as a draft. */
  describe(sessionKey: string, text: string): Promise<void>
  /** What has been said in the making conversation so far. */
  history(sessionKey: string): Promise<PersonaLine[]>
  /** Follow the turn running on it. Answers how to stop following. */
  watch(sessionKey: string, onStep: (step: PersonaStep) => void): Promise<() => void>
  /** The Persona that session generated and has not saved, or null. */
  draft(sessionKey: string): Promise<PersonaRow | null>
  /** Keep it, under `name` when one is given. Answers the name it was kept under. */
  save(sessionKey: string, name?: string): Promise<string>
  /** Let it go. */
  discard(sessionKey: string): Promise<void>
  /** Remove a saved one from the library, for good. */
  remove(name: string): Promise<void>
}
