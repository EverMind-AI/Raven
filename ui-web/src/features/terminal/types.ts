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

export interface TerminalIdentity extends Pick<IdentityRecord, 'agentName' | 'brand' | 'bindingGeneration' | 'taskRef'> {}

export interface MailboxOverviewParams {
  task_id: string
  workspace_id: string
  terminal_handle?: string | null
}

export interface MailboxBinding {
  binding_id: string
  ref: {
    authority_id: string
    tenant_id: string
    agent_id: string
    instance_id: string
    generation: number
  }
  scope: {
    task_id: string
    workspace_id: string
  }
  agent_name: string
  registry_generation: number
  terminal_handle: string | null
  terminal_incarnation: string | null
  session_key: string | null
  capabilities: string[]
}

export interface MailboxArtifactEvidence {
  sha256: string
  size: number
  media_type?: string
  name: string
}

export interface MailboxEnvelopeData {
  protocol_version?: string
  message_id: string
  trace_id?: string
  in_reply_to?: string | null
  kind: string
  sender_identity: {
    authority_id?: string
    tenant_id?: string
    agent_id: string
    instance_id?: string | null
  }
  target_identity: {
    authority_id?: string
    tenant_id?: string
    agent_id: string
    instance_id?: string | null
  }
  scope: {
    task_id: string
    workspace_id: string
  }
  created_at?: string
  ttl?: number
  receipt_policy?: string
  payload?: {
    content_type?: string
    schema?: string
    data?: Record<string, unknown>
  }
  artifacts?: MailboxArtifactEvidence[]
  digest?: {
    algorithm?: string
    canonicalization?: string
    value: string
  }
}

export interface MailboxHandoffStatus {
  status: 'accept_received' | 'PROPOSED' | 'CONFIRMED' | string
  unresolved_items?: string[]
  read_coverage?: Record<string, { bytes_read: number }> | Record<string, unknown>
  offer_message_id?: string
  accept_message_id?: string
}

export interface MailboxMessageRow {
  message_id: string
  digest?: string
  phase: string
  result_hash: string | null
  terminal_reason: string | null
  outcome: string | null
  direction?: 'incoming' | 'outgoing'
  handoff?: MailboxHandoffStatus | null
  envelope?: MailboxEnvelopeData
  attempt: number
  error?: string
}

export interface MailboxNotificationRow {
  request_id: string
  binding_id: string
  message_ids: string | string[]
  input_hash: string
  stage: string
  terminal_incarnation?: string | null
  bytes_written?: number
  turn_id?: string | null
  detail?: string | null
  created_at?: number
  updated_at?: number
}

export interface TaskAuthorityRow {
  task_id: string
  workspace_id: string
  owner_agent_id: string
  assignment_epoch: number
  confirmed_handoff_id: string | null
  offer_message_id: string | null
  accept_message_id: string | null
  updated_at?: number
}

export interface MailboxOverviewData {
  bindings: MailboxBinding[]
  messages: MailboxMessageRow[]
  notifications: MailboxNotificationRow[]
  authority: TaskAuthorityRow | null
}

export interface MailboxOverviewResult {
  data: MailboxOverviewData
}

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
  mailboxOverview?(params: MailboxOverviewParams): Promise<MailboxOverviewResult>
  onOutput: ((frame: TerminalOutputFrame) => void) | null
  onEvent: ((event: TerminalEvent) => void) | null
}
