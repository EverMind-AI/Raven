/* One diff row, in the legacy shape the demo shell's hunk builders still
   produce: a tuple of [kind, text, oldLineNo, newLineNo], where a 'gap' row
   carries the folded lines as its second slot and an `open` expando the
   reader toggles in place. */
export type DiffRow = [string, string | string[], (number | null)?, (number | null)?] & {
  open?: boolean
}

export interface WsHunk {
  rows: DiffRow[]
  add: number
  del: number
}

export interface WsChange {
  key: string
  dir: string
  name: string
  kind: string
  add: number
  del: number
  hunks: WsHunk[]
  turn: number
  open: boolean
  auto?: boolean
  seen?: boolean
  flash?: boolean
}

export interface WsUrl {
  url: string
  kind: string
  at: string
}

export interface WsFile {
  path: string
  downloadPath?: string
  kind: string
  raw: boolean
  text: string | null
  err: string | null
  size: number | null
  loading: boolean
  /* Island-only identity: two opens of the same path are two fresh views. */
  seq?: number
}

/* The workspace state parked with an in-flight conversation. Arrays stay
   shared with the detached turn while it is away; restore replaces the five
   fields on the store's stable record. */
export interface WorkspaceSnapshot {
  changes: WsChange[]
  urls: WsUrl[]
  file: WsFile | null
  turn: number
  unseen: number
}

/* The stable workspace record owned by the island store. */
export type WsShared = WorkspaceSnapshot

export interface FtEntry {
  name: string
  dir?: boolean
  size?: number
}

export type FtKids = FtEntry[] | { err: string }

/* The DS.workspace contract. The fixture source (demo shell) offers only
   shortPath and the demo toast; the rpc source (live layer) adds the fs.*
   surface and flags it with canBrowse -- which is how the island knows to
   draw the real file view instead of the demo's empty note. */
export interface WorkspaceSource {
  shortPath(p: string): string
  canBrowse?: boolean
  list?(dir: string): Promise<{ root?: string; entries: FtEntry[] }>
  reveal?(path: string): Promise<unknown>
  /* Hand the file to an application on the gateway's host. `app` is an
     application NAME the reader chose, or absent for the host default. */
  openIn?(path: string, app?: string): Promise<unknown>
  /* Whether that host is the reader's own desktop. Absent means unknown, which
     is treated as not local: offering to launch a program on somebody else's
     machine is worse than not offering. */
  hostIsLocal?(): boolean
  openPath?(p: string): void
}
