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

/* What one draw reads: the list plus the three page facts a row's look
 * depends on (the current session, whether a turn is running, the search
 * term the chrome's find box holds).
 */
export interface RailSnapshot {
  rows: SessRow[]
  cur: string | null
  busy: boolean
  query: string
}

export interface RailSource {
  snapshot(): RailSnapshot
}
