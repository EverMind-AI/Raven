/** Icons shared by the floating desk and its panes. */

import type { DeskTab } from './types'
import type { JSX } from 'react'

/* Tabs and panes share this table: a pane's kind is not a tab (there is no
   `file` tab any more, and no `deliverables` pane -- a deliverable opens as the
   file it is), so the parameter is the union rather than either one. `agents`
   is now only ever a pane: the tab it used to name became `tasks`, and a
   delegated run still opens as a pane from the transcript's graph card. */
export function DeskIcon({ kind }: { kind: DeskTab | 'file' | 'agents' }): JSX.Element {
  /* The prototype's own `ICONS.bot`, on both: `agents` is the pane header for
     delegated work and `tasks` is the tab that opens it, the same glyph the
     prototype keeps on its tab AND its empty state (proto.js:4355, :4478) --
     not the "graph of steps" shape (`ICONS.task`) the prototype never puts
     on this tab. */
  if (kind === 'tasks' || kind === 'agents') return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <rect x="5" y="7" width="14" height="11" rx="3" />
      <path d="M9 12h.01M15 12h.01M12 7V4M9 18v2M15 18v2" />
    </svg>
  )
  if (kind === 'file') return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M3.5 6.5h6l2 2h9v10h-17z" />
    </svg>
  )
  if (kind === 'deliverables') return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinejoin="round" aria-hidden="true">
      <path d="M3.5 8.5 12 4l8.5 4.5v7L12 20l-8.5-4.5z" />
      <path d="M3.5 8.5 12 13l8.5-4.5M12 13v7" />
    </svg>
  )
  if (kind === 'diff') return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M7 4h10a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z" />
      <path d="M8 12h8" />
      <path d="M12 8v8" />
    </svg>
  )
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <rect x="4" y="4" width="16" height="16" rx="3" />
      <path d="M8 12h8M12 8v8" />
    </svg>
  )
}
