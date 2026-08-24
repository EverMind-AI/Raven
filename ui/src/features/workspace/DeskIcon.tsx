/** Icons shared by the floating desk and its panes. */

import type { DeskTab } from './deskTypes'
import type { JSX } from 'react'

export function DeskIcon({ kind }: { kind: DeskTab }): JSX.Element {
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
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <rect x="4" y="4" width="16" height="16" rx="3" />
      <path d="M8 12h8M12 8v8" />
    </svg>
  )
}
