/* -- workspace: the rpc source and the prose chips' resolvers ---------
   The renderer is the workspace island (ui-web/src/features/workspace/); this
   module only knows how to speak fs.* over /rpc and how to resolve the paths
   an answer makes clickable. The live layer installs both sources onto the
   seam, which replaces the fixture ones before the first paint. */

import type { WorkspaceSource } from './types'
import type { ProseSource, ProseTarget } from '../../lib/prose'

import { islands } from '../../islands'
import { current as sessionCurrent } from '../../lib/session'
import { gateway } from '../../state/gateway'

export function relToWorkspace(p: string | null | undefined): string | null {
  const s = String(p || '')
  const m = s.match(/(?:^|\/)(?:\.raven\/)?workspace\/(.+)$/)
  if (m) return m[1] as string
  if (s.startsWith('/') || s.startsWith('~')) return null
  return s.replace(/^\.\//, '')
}

/* The session's working directory, taken from the session's own init bundle
   (`info.cwd`, set by wsSetRoot below). It used to be learned from fs.list's
   answers, which meant the shortener only worked once something had browsed
   the tree; the resume that opens the session has always carried it.

   Still one value for the page rather than one per conversation: the runtime
   that owns a conversation's state is stage B5's. */
let wsRoot = ''

export const wsSetRoot = (cwd: unknown): void => {
  wsRoot = typeof cwd === 'string' ? cwd.replace(/\/+$/, '') : ''
}

export const relToWsRoot = (p: string | null | undefined): string | null => {
  const s = String(p || '')
  return wsRoot && s.startsWith(wsRoot + '/') ? s.slice(wsRoot.length + 1) : null
}

/* What the workspace panel shows as a short path, and what its own tests
   shorten with. Handed in by the boot's own wiring (state/install.ts), which
   reads it off the installed source. */
let shorten: (p: string) => string = (p) => p
export function setShortener(fn: (p: string) => string): void {
  shorten = fn
}

/* "Looks like a path" is not enough to make one clickable: most repo-shaped
   strings in an answer are not files, and a link that opens onto an error is
   worse than plain text. Two cases are provable without a round trip: the text
   names the workspace, or Raven touched that file this session -- and since the
   viewer is no longer confined to the workspace, the second case now counts
   wherever the file lives. Everything else stays plain text. */
export const livePathOf = (s: string): string | null => {
  const text = String(s).trim().replace(/:\d+(?::\d+)?$/, '')
  if (!text || /\s/.test(text)) return null
  if (/(?:^|\/)(?:\.raven\/)?workspace\/./.test(text)) return relToWorkspace(text) || text
  const hit = islands.workspace.changes().find((c) => c.key === text || shorten(c.key) === text)
  return hit ? hit.key : null
}

/* An explicit markdown link is the author handing something over, so the live
   resolver is broader than the bare-span one above: absolute paths, ~ paths
   and workspace-relative shapes all count, extension decides file-or-folder.
   file:// is stripped rather than rejected -- models write it out of habit. */
export const liveLinkTargetOf = (u: string): ProseTarget | null => {
  let text = String(u).trim().replace(/^file:\/\//, '')
  if (!text || /\s|[<>"']/.test(text)) return null
  const dirMark = /\/$/.test(text)
  text = text.replace(/\/+$/, '')
  if (!/\//.test(text)) return null
  /* Segments take any non-separator character: deliverables are routinely
     named in the reader's own language, and \w-only segments silently dropped
     every one of those back to plain text. */
  const shaped = /^(?:\/|~\/)/.test(text) || /^[^\s/\\]+(?:\/[^\s/\\]+)+$/.test(text)
  if (!shaped) return null
  const dir = dirMark || !/\.\w{1,8}$/.test(text)
  /* A directory link only reaches the host's file manager, which is only there
     when the gateway is this desktop. Anywhere else it would open nothing, and
     plain text beats a link that does nothing. */
  if (dir && !hostIsLocal()) return null
  return { p: text, dir }
}

/* Whether an application launched on the gateway's host would appear on the
   reader's own screen. `open` and `reveal` run where the gateway runs, so on a
   remote serve they would drive somebody else's machine. */
export const hostIsLocal = (): boolean => /^(127\.0\.0\.1|localhost|\[::1\])$/.test(location.hostname)

export const proseSource: ProseSource = {
  pathOf: livePathOf,
  linkTargetOf: liveLinkTargetOf,
  /* A folder is not something the page can show any more -- the file tree went
     with the deliverables shelf -- so it goes to the host's own file manager,
     and only while that host is this desktop. Everywhere else `linkTargetOf`
     stops calling a directory a link at all (see the dir check there). */
  open: ({ p, dir }) => (dir
    ? void gateway().call('fs.reveal', { path: p, session: sessionCurrent() || '' }).catch(() => {})
    : islands.workspace.showFile(p)),
}

/* The gateway host's OS family, handed in by the boot's own wiring
   (state/install.ts), which learns it from the handshake. */
let hostPlatformLive: () => string = () => ''
export function setHostPlatformReader(fn: () => string): void {
  hostPlatformLive = fn
}

export const workspaceSource: WorkspaceSource = {
  hostPlatform: () => hostPlatformLive(),
  canBrowse: true,
  reveal: (p) => gateway().call('fs.reveal', { path: p, session: sessionCurrent() || '' }),
  /* The gateway's own registry of what this conversation handed over. The
     shelf is built from the manifests on turn events while a client watches;
     this is what it is built from when nobody was watching. */
  deliverables: (key) => gateway().call('deliverables.list', { session_key: key }).then((r) => (r && r.files) || []),
  /* The other half of the viewer: a kind the page cannot render goes to the
     host's own application for it. `app` is a name the reader picked, or
     absent for the host default. Only offered while the gateway IS this
     desktop -- see hostIsLocal above. */
  openIn: (p, app) => gateway().call('fs.open', { path: p, session: sessionCurrent() || '', ...(app ? { app } : {}) }),
  hostIsLocal,
  shortPath: (p) => relToWsRoot(p) || relToWorkspace(p) || String(p).replace(/^\/Users\/[^/]+\//, '~/'),
}
