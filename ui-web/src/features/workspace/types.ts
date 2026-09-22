/* The diff-row and hunk shapes live with their builders now (src/lib/hunks.ts,
   a pure leaf two domains share); re-exported here for this domain's own
   readers. */
import type { DiffRow, WsHunk } from '../../lib/hunks'

export type { DiffRow, WsHunk }

export interface WsChange {
  key: string
  dir: string
  name: string
  /* A file the turn created, a whole-file write over one that existed, an
     in-place edit, or a file the turn removed -- the four a change row draws a
     different glyph for. */
  kind: 'add' | 'write' | 'edit' | 'delete'
  add: number
  del: number
  hunks: WsHunk[]
  /* The file as this turn last left it, followed call by call: a whole-file
     write is the file, an edit is applied to it. Null once an edit could not be
     applied to what was being followed, and absent on a row the turn only
     edited -- either way nothing here can say what the file holds. Read only
     when the file is removed and the runtime caught none of its contents. */
  body?: string | null
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
  kind: string
  raw: boolean
  text: string | null
  err: string | null
  size: number | null
  loading: boolean
  /* Island-only identity: two opens of the same path are two fresh views. */
  seq?: number
}

/* One file this session handed over through `deliver_files`. `missing` is the
   runtime's answer on a replay (it stats each path) and the viewer's on an open
   that found nothing; `turn` is which turn delivered it. */
export interface DeliveryRow {
  path: string
  name: string
  title: string
  description: string
  ext: string
  size: number
  mediaType: string
  downloadPath: string
  missing: boolean
  /* Which turn delivered it, or null for a row that came from the gateway's
     own registry rather than from a manifest on a turn event -- the registry
     is what a conversation delivered, not when in the reading it happened. */
  turn: number | null
  /* Which stream delivered it: `deliveries.SESSION` for the conversation's own,
     `agent:<key>` for a delegated one. Declared here rather than kept inside the
     registry because `turn` cannot be read without it -- each stream counts its
     turns from one, so a delegated row's `7` is not this conversation's seventh
     and must not be drawn or grouped as though it were.

     Optional because a row handed to `restore` from outside the registry may not
     carry one, and that one belongs to the conversation; every row the registry
     gives back has it, filled in on the way in. */
  scope?: string
}

/* The stable workspace record owned by the island store. */
export interface WsShared {
  changes: WsChange[]
  urls: WsUrl[]
  file: WsFile | null
  turn: number
  unseen: number
}

/* The workspace state parked with an in-flight conversation. Arrays stay
   shared with the detached turn while it is away; restore replaces the five
   fields on the store's stable record. Deliveries ride along because a parked
   conversation is restored without replaying its history, which is the only
   other place they come from. */
export interface WorkspaceSnapshot extends WsShared {
  deliveries: DeliveryRow[]
}

/* One directory as `fs.dirs` lists it: where the browser stands, one level up
   (null at the filesystem root), where it started, whether a conversation may
   be pinned here, and the subdirectories -- each with the same yes-or-no. */
export interface DirEntry {
  name: string
  path: string
  ok: boolean
}

export interface DirListing {
  path: string
  parent?: string | null
  home: string
  ok: boolean
  entries: DirEntry[]
}

/* The DS.workspace contract. A page with no host behind it offers only
   shortPath and the demo toast; the rpc source (live layer) adds the fs.*
   surface and flags it with canBrowse -- which is how the island knows to
   draw the real file view instead of the demo's note. */
export interface WorkspaceSource {
  shortPath(p: string): string
  /* The gateway host, not the browser: reveal and open-in-app run there. */
  hostPlatform(): string
  /* Whether this source can read a file for the viewer to render. */
  canBrowse?: boolean
  /* What this conversation has handed over, from the gateway's own registry.
     The manifests on turn events say the same thing while a client is
     connected and watching; this answers after a reconnect, after a compaction
     archived the turn that carried one, and for a client that was not open. */
  deliverables?(sessionKey: string): Promise<unknown>
  reveal?(path: string): Promise<unknown>
  /* Hand the file to an application on the gateway's host. `app` is an
     application NAME the reader chose, or absent for the host default. */
  openIn?(path: string, app?: string): Promise<unknown>
  /* Whether that host is the reader's own desktop. Absent means unknown, which
     is treated as not local: offering to launch a program on somebody else's
     machine is worse than not offering. */
  hostIsLocal?(): boolean
  openPath?(p: string): void
  /* The subdirectories of one absolute directory, the reader's home when none
     is named: what the composer's working-directory picker walks
     (state/workdir.ts). Optional like `reveal`: a page with no gateway behind
     it says so on the menu instead of offering a browser over nothing. */
  dirs?(path?: string): Promise<DirListing>
}
