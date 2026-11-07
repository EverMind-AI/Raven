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

/* The widest spelling of each shape, in seconds: 59s, 9m 59s, 59m 59s, 9h 59m, 99h 59m. */
const WIDEST = [59, 599, 3599, 35_999, 359_999]

/** The spellings a count rising to `ms` needs room for: its own, the instant
 *  before its leading figure last turned over (4m 59s under 5m 3s), and each
 *  shorter shape at its widest. A count is not always widest at its end --
 *  45m 30s is wider than 5h 55m -- so a cell that must not move holds them all. */
export function widestDurations(ms: number): string[] {
  const t = Math.max(0, Math.floor(ms / 1000))
  const turned = t >= 3600 ? t - (t % 3600) - 60 : t >= 60 ? t - (t % 60) - 1 : t
  const at = [t, turned, ...WIDEST.filter((mark) => mark < t)]
  return [...new Set(at.map((seconds) => formatDuration(seconds * 1000)))]
}
