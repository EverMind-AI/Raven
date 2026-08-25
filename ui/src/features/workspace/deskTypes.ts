/** Shared types for the floating workspace desk. */

import type { AgentRow, InstanceRow } from '../subagents/types'
import type { WsChange, WsFile } from './types'

export type DeskTab = 'diff' | 'deliverables' | 'agents'

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

export interface DeskState {
  tab: DeskTab
  paletteOpen: boolean
  panes: DeskPane[]
  solo: string | null
  active: string | null
  splits: DeskSplits
}

export interface DeskGeometry {
  x: number
  y: number
  w: number
  h: number
  detached?: boolean
}
