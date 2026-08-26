/** Shared types for the floating workspace desk. */

import type { AgentRow, InstanceRow } from '../subagents/types'
import type { WsChange, WsFile } from './types'

export type DeskTab = 'diff' | 'deliverables' | 'agents'

/* Which way a two-pane desk is cut. The counts above two have one layout each,
   so this says nothing there; at two it is the difference between a stack and a
   pair side by side, which the reader sets by dragging a pane to an edge. */
export type DeskDuo = 'rows' | 'cols'

export type DeskPane =
  | { id: string; kind: 'diff'; change: WsChange }
  | { id: string; kind: 'file'; file: WsFile }
  | { id: string; kind: 'agent'; row: InstanceRow }
  | { id: string; kind: 'agent-record'; row: AgentRow }

export interface DeskSplits {
  column: number
  left: number
  right: number
}

/* How much each tab held the last time the reader looked at it. What is new is
   the difference, which is what the tab's bubble says -- one rule for all three
   rather than a badge per tab inventing its own idea of "new".

   Held in `marks.ts` and parked with the conversation, not in `DeskState`: a
   session switch resets the desk, and a mark reset beside a list that was
   restored reports the whole list as new. */
export type DeskMarks = Record<DeskTab, number>

export interface DeskState {
  tab: DeskTab
  paletteOpen: boolean
  panes: DeskPane[]
  solo: string | null
  active: string | null
  splits: DeskSplits
  duo: DeskDuo
}

export interface DeskGeometry {
  x: number
  y: number
  w: number
  h: number
  detached?: boolean
}
