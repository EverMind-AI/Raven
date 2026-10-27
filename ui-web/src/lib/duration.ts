/* The one compact elapsed-time spelling shared by page rows and graphs.
   Matches the TUI's `fmtDuration` (ui-tui/src/domain/messages.ts) branch for
   branch: whole seconds under a minute, minutes and unpadded seconds past it,
   hours and minutes past an hour. This replaces the prototype's earlier
   zero-padded, minutes-only spelling, so one instant reads the same in the
   terminal and on the page. */

export function formatDuration(ms: number): string {
  const t = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const s = t % 60
  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`
}
