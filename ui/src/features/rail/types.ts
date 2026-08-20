/* A row of the session rail. The objects themselves live in the shared
 * SESS array the legacy layers keep mutating in place (the turn machinery
 * flips `status`, the schedule pages unshift rows, settings wipes it), so
 * every field is the loose shape those writers actually produce.
 */
export interface SessRow {
  id: string
  title: string
  last?: string
  when?: string
  at?: number
  pin?: boolean
  from?: string
  job?: string
  run?: string | null
  live?: boolean
  status?: string | null
}

/* What one draw reads: the list plus the two page facts a row's look depends
 * on (the current session, and whether a turn is running). The search term is
 * NOT in here: it belongs to the row that produces it (shell/find.ts), and
 * asking the demo and live sources to carry a value only the bundle can
 * produce was coupling with nothing on the other end of it.
 */
export interface RailSnapshot {
  rows: SessRow[]
  cur: string | null
  busy: boolean
}

export interface RailSource {
  snapshot(): RailSnapshot
}
