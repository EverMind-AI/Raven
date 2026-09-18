/** Icons shared by the floating desk and its panes. */

import type { DeskTab } from './types'
import type { JSX } from 'react'

/* Tabs and panes share this table: a pane's kind is not a tab (there is no
   `file` tab any more, and no `deliverables` pane -- a deliverable opens as the
   file it is), so the parameter is the union rather than either one. `agents`
   is now only ever a pane: the tab it used to name became `tasks`, and a
   delegated run still opens as a pane from the transcript's graph card. */
export function DeskIcon({ kind }: { kind: DeskTab | 'file' | 'agents' }): JSX.Element {
  if (kind === 'tasks') return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <rect x="3.5" y="4.5" width="7" height="7" rx="2" />
      <rect x="13.5" y="12.5" width="7" height="7" rx="2" />
      <path d="M7 11.5v4a2 2 0 0 0 2 2h4.5" />
    </svg>
  )
  if (kind === 'agents') return (
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
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <rect x="4" y="4" width="16" height="16" rx="3" />
      <path d="M8 12h8M12 8v8" />
    </svg>
  )
}
