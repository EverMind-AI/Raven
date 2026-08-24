/* The one compact elapsed-time spelling shared by page rows and graphs. */

export function formatDuration(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000))
  const pad = (n: number): string => String(n).padStart(2, '0')
  if (seconds >= 3600) {
    return `${Math.floor(seconds / 3600)}h${pad(Math.floor(seconds % 3600 / 60))}m${pad(seconds % 60)}s`
  }
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m${pad(seconds % 60)}s`
  return ms < 10000 ? `${(ms / 1000).toFixed(1)}s` : `${seconds}s`
}
