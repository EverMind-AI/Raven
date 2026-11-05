/* The session's working directory, and the path a delivery lookup compares on.

   The delivery registry keys what the gateway resolved -- an absolute path --
   while the panel holds the path as the model typed it, usually relative to
   the session's working directory. Both name one file, and the registry
   compares them, so the spelling a lookup settles on is the caller's to
   settle: a reader that resolved the path and a writer that did not would
   answer differently about the same file, and only one of the two is looked
   at.

   A leaf on purpose. `source.ts` imports the workspace store, and the store
   resolves a path too -- a delivery the reader finds gone is marked from
   there -- so the resolution cannot live in `source.ts` without a cycle
   between the two. Nothing here imports anything.
*/

let wsRoot = ''

/* The session's working directory, taken from the session's own init bundle
   (`info.cwd`, set by wsSetRoot below). It used to be learned from fs.list's
   answers, which meant the shortener only worked once something had browsed
   the tree; the resume that opens the session has always carried it.

   Still one value for the page rather than one per conversation: the runtime
   that owns a conversation's state is stage B5's. */
export const wsSetRoot = (cwd: unknown): void => {
  /* A lone slash is the root, not an unknown one: stripping it left the page
     unable to resolve a relative path under a session rooted at "/". */
  wsRoot = typeof cwd === 'string' ? (cwd === '/' ? '/' : cwd.replace(/\/+$/, '')) : ''
}

/* The path the panel shows relative to that root, or null when the path is
   not under it at all. */
export const relToWsRoot = (p: string | null | undefined): string | null => {
  const s = String(p || '')
  if (!wsRoot) return null
  const prefix = wsRoot === '/' ? '/' : wsRoot + '/'
  return s.startsWith(prefix) ? s.slice(prefix.length) : null
}

/* The path a delivery lookup compares against. The registry keys what the
   gateway resolved -- an absolute path -- while the panel opens a file by the
   path the model typed, usually relative to the session's working directory
   (record.ts's `sameFile` reconciles the same two spellings for change rows).
   Resolving the relative side here lets deliveries.byPath find the row without
   falling back to a bare file name, which could hand back another file's
   download token.

   Every lookup on a path the PANEL holds goes through here first -- the reads
   and the writes alike. */
export const absPath = (p: string): string => {
  const s = String(p || '').replace(/\\/g, '/')
  if (!s || s.startsWith('/') || /^[A-Za-z]:\//.test(s) || s.startsWith('~')) return s
  return wsRoot ? `${wsRoot.replace(/\\/g, '/').replace(/\/+$/, '')}/${s}` : s
}

/* Test seam only: the session root is the module's, and a test that sets one
   gives it back. */
export const _resetForTests = (): void => {
  wsRoot = ''
}
