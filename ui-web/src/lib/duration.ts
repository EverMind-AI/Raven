/* The one compact elapsed-time spelling shared by page rows and graphs.
   Two shapes only, matching the prototype's own `fmtDur` exactly: whole
   seconds under a minute, minutes and zero-padded seconds past it -- no
   sub-second decimal and no hour tier, so every surface that shows a
   duration (a list row, a status bar, a graph card) reads the same number
   the prototype would have written. */

export function formatDuration(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000))
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`
}
