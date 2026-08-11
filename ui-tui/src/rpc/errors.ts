// Raven TUI RPC — typed error class hierarchy.
//
// Mirrors the 15 server-defined error codes in specs/tui-ipc.md §4.
// `rpcErrorFromFrame(frame)` is the canonical constructor used by `client.ts`
// when a JSON-RPC error response arrives — it selects the matching subclass
// by `code`, falling back to the generic `RpcError` for unknown codes.

import type { JsonRpcErrorObject } from './generated.js'

/** Fields the server puts in `error.data` to explain a failure (see
 *  `raven/tui_rpc/errors.py` and `_build_tui_agent_loop`). `frame.message` is a
 *  fixed code name like `internal_error`, so without these the user is told
 *  nothing actionable. */
const DETAIL_KEYS = ['detail', 'exception_message', 'reason'] as const

const readString = (data: Record<string, unknown>, key: string): string | undefined => {
  const value = data[key]
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

/** `[rpc -32603] internal_error: <cause> (see ~/.raven/logs/tui.log)`.
 *  Callers render `err.message` directly, so the cause has to live there. */
export function formatRpcError(frame: JsonRpcErrorObject): string {
  let text = `[rpc ${frame.code}] ${frame.message}`
  if (typeof frame.data !== 'object' || frame.data === null || Array.isArray(frame.data)) {
    return text
  }
  const data = frame.data as Record<string, unknown>
  const detail = DETAIL_KEYS.map(key => readString(data, key)).find(Boolean)
  // A one-line detail reads inline; a multi-line one (a config error listing
  // every offending field, say) keeps its shape below the summary.
  if (detail) {
    text += detail.includes('\n') ? `:\n${detail}` : `: ${detail}`
  }
  const logPath = readString(data, 'log_path')
  if (logPath) {
    text += `\n(details in ${logPath})`
  }
  return text
}

/** Base class for all JSON-RPC error responses surfaced to callers. */
export class RpcError extends Error {
  readonly code: number
  readonly data: unknown

  constructor(frame: JsonRpcErrorObject) {
    super(formatRpcError(frame))
    this.name = 'RpcError'
    this.code = frame.code
    this.data = frame.data
  }
}

// -- Server-defined business errors (specs §4) -------------------------------

export class SessionNotFoundError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'SessionNotFoundError'
  }
}
export class SessionLockedError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'SessionLockedError'
  }
}
export class TurnInProgressError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'TurnInProgressError'
  }
}
export class McpServerNotConnectedError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'McpServerNotConnectedError'
  }
}
export class McpToolCallFailedError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'McpToolCallFailedError'
  }
}
export class SkillNotFoundError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'SkillNotFoundError'
  }
}
export class SkillPinConflictError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'SkillPinConflictError'
  }
}
export class ModelNotAvailableError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'ModelNotAvailableError'
  }
}
export class ModelSwitchInTurnError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'ModelSwitchInTurnError'
  }
}
export class ConfigFieldReadonlyError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'ConfigFieldReadonlyError'
  }
}
export class ConfigValidationError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'ConfigValidationError'
  }
}
export class NotSupportedInV01Error extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'NotSupportedInV01Error'
  }
}
export class CliCommandFailedError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'CliCommandFailedError'
  }
}
export class CliCommandTimeoutError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'CliCommandTimeoutError'
  }
}
export class NotDispatchCompatibleError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f)
    this.name = 'NotDispatchCompatibleError'
  }
}

// -- code → subclass mapping -------------------------------------------------

const CODE_TO_CTOR: Record<number, new (f: JsonRpcErrorObject) => RpcError> = {
  [-32001]: SessionNotFoundError,
  [-32002]: SessionLockedError,
  [-32003]: TurnInProgressError,
  [-32004]: McpServerNotConnectedError,
  [-32005]: McpToolCallFailedError,
  [-32006]: SkillNotFoundError,
  [-32007]: SkillPinConflictError,
  [-32008]: ModelNotAvailableError,
  [-32009]: ModelSwitchInTurnError,
  [-32010]: ConfigFieldReadonlyError,
  [-32011]: ConfigValidationError,
  [-32012]: NotSupportedInV01Error,
  [-32013]: CliCommandFailedError,
  [-32014]: CliCommandTimeoutError,
  [-32015]: NotDispatchCompatibleError
}

/** Pick the right subclass for an incoming JSON-RPC error frame. */
export function rpcErrorFromFrame(frame: JsonRpcErrorObject): RpcError {
  const Ctor = CODE_TO_CTOR[frame.code]
  return Ctor ? new Ctor(frame) : new RpcError(frame)
}
