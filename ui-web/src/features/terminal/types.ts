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

export interface TerminalSource {
  list(taskId: string): Promise<TerminalListReply>
}
