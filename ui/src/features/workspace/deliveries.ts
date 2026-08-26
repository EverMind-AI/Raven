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
let rows: DeliveryRow[] = []
let version = 0
const listeners = new Set<() => void>()

export const getVersion = (): number => version

/* A row from the registry has no turn, and sorts as older than any turn the
   reader watched happen -- which is what it is: something this conversation
   delivered, recovered rather than witnessed. */
const rank = (row: DeliveryRow): number => (row.turn == null ? -1 : row.turn)

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function bump(): void {
  version += 1
  listeners.forEach((listener) => listener())
}

/* The shelf: one row per path at the turn that last delivered it, newest turn
   first, manifest order inside a turn -- it reads top-down as "what just
   happened, then what came before". */
export function list(): DeliveryRow[] {
  const newest = new Map<string, { row: DeliveryRow; index: number }>()
  rows.forEach((row, index) => {
    const seen = newest.get(row.path)
    if (!seen || rank(row) > rank(seen.row)) newest.set(row.path, { row, index })
  })
  return [...newest.values()]
    .sort((a, b) => rank(b.row) - rank(a.row) || a.index - b.index)
    .map(({ row }) => row)
}

/* What that turn delivered, which is what its own card in the transcript shows.
   Unaffected by anything a later turn delivered. */
export const ofTurn = (turn: number): DeliveryRow[] => rows.filter((row) => row.turn === turn)

/* The newest row for a path: what the pane says about the file that is open. */
export const byPath = (path: string): DeliveryRow | null =>
  rows.reduce<DeliveryRow | null>(
    (best, row) => (row.path === path && (!best || rank(row) > rank(best)) ? row : best),
    null,
  )

/* What the shelf shows, so the tab's number matches its list: paths, not rows. */
export const count = (): number => new Set(rows.map((row) => row.path)).size

/* Parse one delivery manifest. The shape is the tool event's metadata, so a
   caller can hand over whatever it received without checking it first. */
/* One delivered file, from either door: the wire shape is the same in a turn
   event's manifest and in the gateway's registry, because both are written from
   the same record. */
function rowOf(entry: unknown, turn: number | null): DeliveryRow | null {
  if (!entry || typeof entry !== 'object') return null
  const item = entry as Record<string, unknown>
  const path = String(item.path || '')
  if (!path) return null
  const name = String(item.name || path.split('/').pop() || path)
  const dot = name.lastIndexOf('.')
  return {
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

function put(next: DeliveryRow): boolean {
  const at = rows.findIndex((row) => row.turn === next.turn && row.path === next.path)
  if (at >= 0) {
    rows[at] = next
    return true
  }
  rows.push(next)
  return true
}

export function record(turn: number, metadata: unknown): void {
  const root = metadata && typeof metadata === 'object' ? metadata as Record<string, unknown> : null
  const raw = root && root.raven_delivery && typeof root.raven_delivery === 'object'
    ? root.raven_delivery as Record<string, unknown> : null
  const files = raw && Array.isArray(raw.files) ? raw.files : []
  if (!files.length) return
  let changed = false
  files.forEach((entry) => {
    const next = rowOf(entry, turn)
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
  for (let i = files.length - 1; i >= 0; i -= 1) {
    const next = rowOf(files[i], null)
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
  rows = Array.isArray(next) ? next : []
  bump()
}

export function reset(): void {
  if (!rows.length) return
  rows = []
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
