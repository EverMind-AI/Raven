/* The gateway's side-channel pushes, named and typed by hand.
 *
 * rpc-schema/openrpc.json declares calls, not pushes, so the generated client
 * knows nothing about these and a handler registered under a misspelt name is
 * simply never called -- silently, for the life of the tab. The eleven names
 * below are the whole of what this page listens for besides the subscription
 * envelope.
 *
 * Ten are raven/acp/updates.py's SIDE_CHANNEL_METHODS, and
 * scripts/gates/notifications-contract.test.mjs reads that frozenset directly
 * rather than trusting this copy of it. The eleventh, `browser.frame`, is not
 * on the server's list: it is the base64 screencast an older gateway pushes
 * instead of a binary frame, and it is the only name that gate allows here
 * beyond the server's ten.
 *
 * The params are hand-written from the emitting sites, each named in its doc
 * comment. They are not generated and the contract is not changed to carry
 * them: a push has no result, so there is nothing for the generator to bind
 * the two ends of.
 */

/** The eleven, in the order the parts that handle them install. */
export const NOTIFICATION_METHODS = [
  'confirm.request',
  'approval.request',
  'approval.closed',
  'clarify.request',
  'clarify.closed',
  'system.update_available',
  'memory.health',
  'mcp.status',
  'oauth.pending',
  'oauth.done',
  'browser.frame',
] as const

/** A side-channel push by name. */
export type NotificationMethod = (typeof NOTIFICATION_METHODS)[number]

/**
 * A name `gateway().on(...)` accepts: the eleven, plus the subscription
 * envelope. Anything else is a compile error, and
 * scripts/gates/notifications-contract.test.mjs holds this list equal to the
 * gateway's own.
 */
export type PushMethod = NotificationMethod | 'event'

/**
 * A conversation the push names, or null when the entry point cannot name one
 * (a bare `cli.dispatch`). The page falls back to the open conversation, which
 * is what the field exists to avoid wherever it can be filled in.
 */
type ConversationId = string | null

/** raven/rpc/approval_broker.py: the permission gate's ask. */
export interface ApprovalRequestParams {
  approval_id: string
  conversation_id: ConversationId
  turn_id: string
  tool_call_id: string
  command: string
  description: string
  suggested_pattern: string
  /** The layout the sheet draws: shell.exec, file.write, mcp.call or unknown. */
  kind: string
  /** The shell command family that words the prompt, or empty. */
  family: string
  /** Who is asking: the request's origin, and a sub-agent's name. */
  origin: { kind: string; name: string }
  /** The tool's own account of the call, shaped by `kind`. */
  evidence: Record<string, unknown>
}

/**
 * raven/rpc/approval_broker.py. `reason` is the choice a person made, or one
 * of the three nobody chose: "cancelled", "timeout" (a host that set a
 * ceiling), "error". The last two are what the page turns into the lapsed notice.
 */
export interface ApprovalClosedParams {
  approval_id: string
  conversation_id: ConversationId
  reason: string
}

/** One question of an ask_user batch, as the request's `batch` lists them.
    `choices` and its two companions arrive from the ask_user tool; a producer
    that predates them (an ACP form) sends the question and header alone, and
    the sheet then asks that batch one question at a time. */
export interface ClarifyBatchEntry {
  question: string
  header?: string
  choices?: string[]
  recommended?: string
  multi_select?: boolean
}

/** raven/rpc/question_broker.py: the question a tool asks mid-turn. */
export interface ClarifyRequestParams {
  conversation_id: string
  request_id: string
  question: string
  choices: string[]
  header: string
  recommended: string
  multi_select: boolean
  timeout_s: number
  index: number
  total: number
  batch: ClarifyBatchEntry[]
}

/** raven/rpc/question_broker.py: nobody answered it and nobody will. */
export interface ClarifyClosedParams {
  conversation_id: string
  request_id: string
}

/** raven/rpc/confirm_broker.py. `conversation_id` is omitted, not nulled. */
export interface ConfirmRequestParams {
  request_id: string
  prompt: string
  default: boolean
  conversation_id?: string
}

/** raven/rpc/methods/system.py and raven/cli/serve_commands.py. */
export interface SystemUpdateAvailableParams {
  latest_version: string
}

/** raven/mcp/manager.py `_snapshot`: one server's state changed. */
export interface McpStatusParams {
  name: string
  transport: string
  state: string
  connected: boolean
  tool_count: number
  error: string | null
  enabled: boolean
  /** Present on every snapshot, null included, so a merge can clear it. */
  auth_url: string | null
}

/** raven/agent/loop/organ_glue.py: long-term memory stopped writing, or started. */
export interface MemoryHealthParams {
  ok: boolean
  error: string | null
}

/** raven/mcp/oauth.py: an authorization is open, with its own deadline. */
export interface OauthPendingParams {
  server: string
  url: string
  expires_in: number
  interactive: boolean
}

/** raven/mcp/oauth.py: that authorization ended, one way or the other. */
export interface OauthDoneParams {
  server: string
  ok: boolean
  error?: string
}

/**
 * The older screencast frame: an older gateway pushes the page state with the
 * JPEG base64 inside it rather than sending the binary `RVF1` frame. Shaped
 * like `BrowserFrameResult` in raven/rpc/models.py, where every field is
 * optional because the surface grew over several gateway generations.
 */
export interface BrowserFramePushParams {
  ok?: boolean
  url?: string | null
  title?: string | null
  loading?: boolean | null
  vw?: number | null
  vh?: number | null
  jpeg?: string | null
}

/**
 * The subscription envelope. Not a side channel and not on the server's list:
 * its params are `{subscription_id, event}`, one consumer owns it, and it is
 * declared here only because the transport has to accept the name.
 */
export interface StreamEventParams {
  subscription_id: string
  event?: { type?: string; payload?: Record<string, unknown> }
}

/**
 * What each push carries. `extends Record<PushMethod, object>` is the check:
 * a name added to the list above with no params declared here does not
 * compile.
 */
export interface PushParams extends Record<PushMethod, object> {
  event: StreamEventParams
  'confirm.request': ConfirmRequestParams
  'approval.request': ApprovalRequestParams
  'approval.closed': ApprovalClosedParams
  'clarify.request': ClarifyRequestParams
  'clarify.closed': ClarifyClosedParams
  'system.update_available': SystemUpdateAvailableParams
  'memory.health': MemoryHealthParams
  'mcp.status': McpStatusParams
  'oauth.pending': OauthPendingParams
  'oauth.done': OauthDoneParams
  'browser.frame': BrowserFramePushParams
}
