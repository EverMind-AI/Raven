/** Generated wire contracts plus view state for hosted terminal tabs. */

import type {
  A2AAckMatchedEvent,
  A2ASendEvent,
  IdentityRecord,
  TerminalClosedEvent,
  TerminalCreatedEvent,
  TerminalInputParams,
  TerminalListResult,
  TerminalRecord,
  TerminalResizeParams,
  TerminalStatusEvent,
  TerminalSubscribeParams,
  TerminalSubscribeResult,
} from '../../rpc/generated'

export interface TerminalIdentity extends Pick<IdentityRecord, 'agentName' | 'brand' | 'bindingGeneration'> {}

export interface TerminalRow extends TerminalRecord {
  handle: string
  incarnationId: string
  ptyId: string
  tabId: string
  leafId: string
  paneKey: string
  worktreeId: string
  worktreePath: string
  executionHostId: string
  title: string
  status: NonNullable<TerminalRecord['status']>
  liveness: NonNullable<TerminalRecord['liveness']>
  connected: boolean
  writable: boolean
  orphaned: boolean
  visible: boolean
  owner: string
  lastOutputAt: number | null
  identity?: TerminalIdentity
}

export interface TerminalListReply extends Omit<TerminalListResult, 'terminals'> {
  terminals: TerminalRow[]
}

export interface TerminalOutputFrame {
  handle: string
  seq: number
  replay?: boolean
  data: Uint8Array
}

export type TerminalEvent =
  | TerminalCreatedEvent
  | TerminalClosedEvent
  | TerminalStatusEvent
  | A2ASendEvent
  | A2AAckMatchedEvent

export interface TerminalSource {
  list(taskId: string): Promise<TerminalListReply>
  input(params: TerminalInputParams): Promise<unknown>
  resize(params: TerminalResizeParams & { handle: string; cols: number; rows: number }): Promise<unknown>
  subscribe(params: TerminalSubscribeParams): Promise<TerminalSubscribeResult>
  onOutput: ((frame: TerminalOutputFrame) => void) | null
  onEvent: ((event: TerminalEvent) => void) | null
}
