/** The session's delivered files: one registry, two readers.

The rows come from `deliver_files`, which puts its manifest on the tool event's
metadata (`raven_delivery`); a live turn records them as the event arrives and a
reload records them from the replayed history, where the runtime has already
stamped each file with whether it is still on disk.

The registry lives here rather than with either reader because there are two: the
transcript draws the turn's own delivery card, and the desk draws the session's
shelf. It imports nothing from either -- the workspace store parks and restores
it, the transcript writes into it, the desk reads it.
*/

import type { DeliveryRow } from './types'

/* One row per (turn, path), in arrival order -- which is manifest order within
   a turn. Per turn, not per path: `ofTurn` is what the transcript's per-turn
   card reads, and a row that moved its own turn forward when the file was
   delivered again took that turn's card away with it (the section, and the
   whole products card when the turn changed no files). Collapsing by path is
   the shelf's business, and happens in `list`/`byPath` below. */
/* Which stream delivered it. The empty string is the conversation itself; a
   delegated run gets one of its own, because its turns are numbered from one
   like everybody else's and `ofTurn` would otherwise hand a sub-agent's card
   the files the conversation underneath delivered in its first turn -- and hand
   the conversation's card the sub-agent's. The scope is what keeps the two
   apart. It used to be an emptied registry instead, which kept them apart by
   leaving only one of them anything to read: every repaint of a delegated
   stream threw away the deliveries the conversation was still showing.

   Only `ofTurn` is scoped. The session-wide readers -- the shelf, the pane, the
   tab's count -- are about files this session handed over, whoever handed them
   over, and read every row. */
type Held = DeliveryRow & { at: number; when: number | null; call: string }

let rows: Held[] = []
/* When a row arrived, which is the only clock the streams share. Their turn
   numbers are not one: a delegated run counts from one exactly as the
   conversation does, so a sub-agent's tenth turn read as newer than the
   conversation's second -- the shelf put the older row on top, and a path both
   of them delivered resolved to the older one's title and token. Arrival is
   comparable across streams because there is one of it. */
let arrivals = 0
/* The arrivals of the rows the last `dropScope` took, by the identity `put`
   keys on. The conversation's rows are dropped and re-recorded on every main-lane
   replay, and for an unstamped row arrival is the only key it has -- re-recording
   handed it a fresh higher one while a sub-agent's stayed put, so the same two
   deliveries answered differently before and after a replay. Handing the arrival
   back makes the replay what it claims to be: the same rows, read again.

   Holds one drop at a time. It exists for the re-record that follows immediately,
   so the next drop replaces it and a fresh registry clears it. */
let dropped = new Map<string, number>()
let version = 0
const listeners = new Set<() => void>()

export const getVersion = (): number => version

/* Newest first, on one clock.
 *
 * How the session-wide readers -- the shelf and the pane -- order rows they may
 * have taken from different streams. A row from the gateway's registry has no
 * turn, and sorts as older than anything the reader watched arrive, which is
 * what it is: something this conversation delivered, recovered rather than
 * witnessed.
 *
 * `when` is the manifest's own `delivered_at` -- written by the gateway when the
 * delivery happened, in epoch milliseconds UTC, and carried by the identical
 * object down both paths: the live event and the replayed message. So there is
 * one clock here and no inference about where a row came from. Ordering by paint
 * order inverted a lazily replayed history against a newer delivery; ordering a
 * stored stamp against the browser's clock compared two clocks; ordering live
 * above replayed encoded the event source as chronology, and a background
 * sub-agent that delivers later but paints afterwards is exactly the case that
 * breaks. A field written where the delivery happens is the only thing that is
 * none of those.
 *
 * A manifest with no `delivered_at` predates that field and sorts oldest, as does
 * a row recovered from the gateway's registry -- something this conversation
 * delivered, recovered rather than witnessed.
 *
 * Two STAMPED rows can only share a stamp by coming from different producers,
 * since one producer's stamps never repeat. There is no chronological fact left
 * to read then, so the call that made each one decides -- stable, assigned where
 * the delivery happened, and the same answer however the page came to learn of
 * them.
 *
 * Two UNSTAMPED rows are a different case and must not be sorted that way. They
 * are manifests written before the field existed, so a whole legacy history is
 * unstamped, and their call ids carry no order at all -- ranking by them would
 * pick an arbitrary row where replay order at least follows the transcript. They
 * keep arrival, which is what they had.
 *
 * A total order either way: one key per row, compared the same way every time. */
const later = (row: Held, than: Held): boolean => {
  const seen = row.turn == null
  const other = than.turn == null
  if (seen !== other) return other
  const a = row.when
  const b = than.when
  if (a == null || b == null) {
    /* An unstamped row is older than a stamped one -- the field is newer than it
       is -- and two unstamped ones keep the order they were replayed in. */
    if (a !== b) return b == null
    return row.at > than.at
  }
  if (a !== b) return a > b
  if (row.call !== than.call) return row.call > than.call
  return row.at > than.at
}

const newestFirst = (a: Held, b: Held): number => (later(a, b) ? -1 : later(b, a) ? 1 : 0)

/* The conversation's own stream. Written out rather than left implicit so the
   readers that mean "the session" say so. */
export const SESSION = ''

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function bump(): void {
  version += 1
  listeners.forEach((listener) => listener())
}

/* The shelf: one row per path, at the delivery that handed it over last, newest
   first -- it reads top-down as "what just happened, then what came before". */
export function list(): DeliveryRow[] {
  const newest = new Map<string, { row: Held; index: number }>()
  rows.forEach((row, index) => {
    const seen = newest.get(row.path)
    if (!seen || later(row, seen.row)) newest.set(row.path, { row, index })
  })
  return [...newest.values()]
    .sort((a, b) => newestFirst(a.row, b.row) || a.index - b.index)
    .map(({ row }) => row)
}

/* What that turn delivered, which is what the turn's own products card shows.
   Unaffected by anything a later turn delivered. One row per path: a turn that
   handed the same file over from two calls produced it once, and the card is
   about the turn's products, not about its calls. */
export function ofTurn(scope: string, turn: number): DeliveryRow[] {
  const mine = rows.filter((row) => row.scope === scope && row.turn === turn)
  const newest = new Map<string, Held>()
  mine.forEach((row) => {
    const seen = newest.get(row.path)
    if (!seen || row.at >= seen.at) newest.set(row.path, row)
  })
  return mine.filter((row) => newest.get(row.path) === row)
}

/* The newest row for a path: what the pane says about the file that is open.
   Session-wide, because the pane is -- it shows the file, not the stream that
   last handed it over. */
export const byPath = (path: string): DeliveryRow | null =>
  rows.reduce<Held | null>(
    (best, row) => (row.path === path && (!best || later(row, best)) ? row : best),
    null,
  )


/* What the shelf shows, so the tab's number matches its list: paths, not rows.
   The path is also each row's identity for the seen record -- one file the
   session handed over, however many turns handed it over. */
export const paths = (): string[] => [...new Set(rows.map((row) => row.path))]

export const count = (): number => paths().length

/* Parse one delivery manifest. The shape is the tool event's metadata, so a
   caller can hand over whatever it received without checking it first. */
/* One delivered file, from either door: the wire shape is the same in a turn
   event's manifest and in the gateway's registry, because both are written from
   the same record. */
function rowOf(entry: unknown, turn: number | null, scope: string, at: number,
  call: string, when: number | null): Held | null {
  if (!entry || typeof entry !== 'object') return null
  const item = entry as Record<string, unknown>
  const path = String(item.path || '')
  if (!path) return null
  const name = String(item.name || path.split('/').pop() || path)
  const dot = name.lastIndexOf('.')
  return {
    scope,
    at,
    when,
    call,
    path,
    name,
    title: String(item.title || name),
    description: String(item.description || ''),
    ext: dot > 0 ? name.slice(dot + 1).toLowerCase() : '',
    size: Number(item.size) || 0,
    mediaType: String(item.media_type || ''),
    downloadPath: String(item.download_path || ''),
    missing: item.missing === true,
    turn,
  }
}

/* One row per (stream, delivery, path). The delivery is the call that made it;
   the turn is what it is filed under. Written once and read by both the merge and
   the arrival memory, so those two cannot key on different things. */
const identity = (row: Held): string => `${row.scope}\u0000${row.call}\u0000${row.turn}\u0000${row.path}`

function put(next: Held): boolean {
  /* Keyed by the turn alone, two calls in one turn handing over the same path
     were one row and the earlier card read the later manifest. */
  const key = identity(next)
  const seen = rows.findIndex((row) => identity(row) === key)
  if (seen >= 0) {
    /* The same delivery, recorded again -- live and then replayed from history,
       or a re-read of the same turn. It keeps the arrival it already had: this
       is not the file being handed over a second time, and moving it to the
       front of the shelf would say it was. */
    rows[seen] = { ...next, at: (rows[seen] as Held).at }
    return true
  }
  /* Same delivery, and the row for it was dropped a moment ago rather than being
     here to find -- a replay. It keeps its arrival for the same reason. */
  const before = dropped.get(key)
  rows.push(before == null ? next : { ...next, at: before })
  return true
}

export function record(scope: string, turn: number, metadata: unknown, call = ''): void {
  const root = metadata && typeof metadata === 'object' ? metadata as Record<string, unknown> : null
  const raw = root && root.raven_delivery && typeof root.raven_delivery === 'object'
    ? root.raven_delivery as Record<string, unknown> : null
  const files = raw && Array.isArray(raw.files) ? raw.files : []
  if (!files.length) return
  let changed = false
  /* One arrival for the manifest, not one per file: the files in it were handed
     over together, and the array order they are pushed in is what keeps them in
     manifest order inside their own delivery. */
  arrivals += 1
  const at = arrivals
  /* When the delivery happened, as the gateway wrote it into this manifest. The
     same object reaches the live event and the replayed message, so both read one
     clock; a manifest written before the field existed has none. */
  const stamped = raw ? raw.delivered_at : null
  const moment = typeof stamped === 'number' && Number.isFinite(stamped) ? stamped : null
  files.forEach((entry) => {
    const next = rowOf(entry, turn, scope, at, call, moment)
    if (next) changed = put(next) || changed
  })
  if (changed) bump()
}

/* The gateway's registry for this conversation, oldest first. Merged rather
   than replacing: a manifest row knows which turn it belongs to and the
   registry does not, so a path the reader already watched arrive keeps its
   turn -- the registry only fills in what this client never saw.

   Its rows are added newest-first so that, among themselves, they read in the
   same direction as the turns above them. */
export function seed(files: unknown): void {
  if (!Array.isArray(files)) return
  let changed = false
  arrivals += 1
  for (let i = files.length - 1; i >= 0; i -= 1) {
    /* A row recovered from the gateway's registry sorts before anything the
       reader watched arrive, whatever moment it carries -- `order` answers -1 for
       a row with no turn -- so this only has to be a number. */
    const next = rowOf(files[i], null, SESSION, arrivals, '', null)
    if (!next) continue
    /* Nothing to recover for a path a turn already accounted for. */
    if (rows.some((row) => row.path === next.path && row.turn != null)) continue
    changed = put(next) || changed
  }
  if (changed) bump()
}

/* The reader opened it and it was not there. Written back so the shelf row goes
   grey too, rather than each surface discovering the same absence for itself.
   Every turn's row for that path: the file is gone for all of them, and the
   turn cards would otherwise disagree with the shelf about the same file. */
export function markMissing(path: string): void {
  if (!rows.some((row) => row.path === path && !row.missing)) return
  rows = rows.map((row) => (row.path === path && !row.missing ? { ...row, missing: true } : row))
  bump()
}

/* Park and restore, through the workspace store's snapshot: a conversation
   switched away from and back never replays its history, so without this the
   live turn's deliveries would be gone on return. */
export const snapshot = (): DeliveryRow[] => rows

export function restore(next: DeliveryRow[]): void {
  /* A row that arrives without one belongs to the conversation: the only rows
     written from outside this module are a test's, and the parked snapshot is
     this module's own rows going back where they came from, scope and all. */
  /* The parked rows carry their own arrival back; a row written from outside
     this module (a test) gets one now, and they keep the order they came in. */
  dropped = new Map()
  rows = Array.isArray(next)
    ? next.map((row, i) => ({ at: arrivals + 1 + i, when: null, call: '', ...row, scope: row.scope || SESSION } as Held))
    : []
  arrivals += Array.isArray(next) ? next.length : 0
  bump()
}


/* Everything, for a registry that is starting over. */
export function reset(): void {
  dropped = new Map()
  if (!rows.length) return
  rows = []
  bump()
}

/* One stream's rows, and only one stream's.
 *
 * The one caller is the transcript's replay, which passes `SESSION`: the
 * conversation's own rows are about to be replayed, or it is a different
 * conversation now. What matters is the "only one" -- clearing the whole
 * registry there emptied the deliveries of the conversation underneath every
 * time a reader opened a sub-agent panel.
 *
 * Deliberately not called on lane release. A pane closing is not the delegated
 * stream's rows going away: the shelf is session-wide and records what this
 * conversation delivered, sub-agents included, so a reader who closes a panel
 * would otherwise lose the record of work that really happened. Those rows end
 * where the conversation does -- `resetView` (`live/080-overrides.js`) calls
 * `wsReset`, and the workspace store's `resetShared` restores an empty registry
 * across every scope. */
export function dropScope(scope: string): void {
  if (!rows.some((row) => row.scope === scope)) return
  dropped = new Map(rows.filter((row) => row.scope === scope).map((row) => [identity(row), row.at]))
  rows = rows.filter((row) => row.scope !== scope)
  bump()
}

/* How a delivery's size reads. Shared with the transcript's own card so one
   file is not "86 KB" in one place and "86.3 KB" in the other. */
export function humanSize(bytes: number): string {
  if (!bytes) return ''
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1 }
  return `${unit ? value.toFixed(value < 10 ? 1 : 0) : Math.round(value)} ${units[unit]}`
}
