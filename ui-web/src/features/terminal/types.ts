/** Wire and view contracts for hosted terminal tabs. */

export type TerminalStatus = 'idle' | 'working' | 'permission' | 'unknown'
export type TerminalLiveness = 'live' | 'exited' | 'unverifiable'

export interface TerminalIdentity {
  agentName: string
  brand: string
  bindingGeneration: number
}

export interface TerminalRow {
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
  status: TerminalStatus
  liveness: TerminalLiveness
  connected: boolean
  writable: boolean
  orphaned: boolean
  visible: boolean
  owner: string
  lastOutputAt: number | null
  identity?: TerminalIdentity
}

export interface HostScope {
  hostIds: string[]
  omittedHostIds: string[]
}

export interface TerminalListReply {
  terminals: TerminalRow[]
  truncated: boolean
  hostScope: HostScope
  topologyRevisions: Record<string, number>
  visualLayouts?: Array<Record<string, unknown>>
}

export interface TerminalOutputFrame {
  handle: string
  seq: number
  replay?: boolean
  data: Uint8Array
}

export interface TerminalSubscribeReply {
  subscription: {
    handle: string
    enabled: boolean
    seq: number
    ackBytes: number
    subscription_id?: string
  }
}

export interface TerminalSubscribeParams {
  handle: string
  enabled?: boolean
  ack?: number
}

export interface TerminalSource {
  list(taskId: string): Promise<TerminalListReply>
  input(params: { handle: string; data: string }): Promise<unknown>
  resize(params: { handle: string; cols: number; rows: number }): Promise<unknown>
  subscribe(params: TerminalSubscribeParams): Promise<TerminalSubscribeReply>
  onOutput: ((frame: TerminalOutputFrame) => void) | null
}
