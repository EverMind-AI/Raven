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

  /* The four things a reader can do TO a session, rather than read about one.
     All optional, and the reason is that the offline demo has an answer for
     each of them already: the island's own optimistic behaviour, which is the
     honest thing for a page with no server behind it. A source that installs
     one is saying "there is somewhere to put this", so the island hands the
     action over instead of pretending locally. */

  /* Delete it. The whole flow, not a bare rpc: the live page has a
     confirmation to ask and a good deal of per-session state to forget.
     Absent -- the demo -- and the island removes the row itself, with undo. */
  remove?(s: SessRow): void
  /* The title just changed, through the island's own inline editor. Not
     `rename`: the editing is the island's and always was, and the live layer
     used to reach into the island's DOM to hang a blur listener off the input
     it had created. This is the notification that replaces that. */
  renamed?(id: string, title: string): void
  /* The pin moved. Optimistic in the island; a source that cannot keep the
     flag says so by not being here. */
  pin?(id: string, pinned: boolean): void
  /* Every session, from the settings page's data section. */
  deleteAll?(): void
}
