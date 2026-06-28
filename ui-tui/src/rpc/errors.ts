// Raven TUI RPC — typed error class hierarchy.
//
// Mirrors the 15 server-defined error codes in specs/tui-ipc.md §4.
// `rpcErrorFromFrame(frame)` is the canonical constructor used by `client.ts`
// when a JSON-RPC error response arrives — it selects the matching subclass
// by `code`, falling back to the generic `RpcError` for unknown codes.

import type { JsonRpcErrorObject } from './generated.js';

/** Base class for all JSON-RPC error responses surfaced to callers. */
export class RpcError extends Error {
  readonly code: number;
  readonly data: unknown;

  constructor(frame: JsonRpcErrorObject) {
    super(`[rpc ${frame.code}] ${frame.message}`);
    this.name = 'RpcError';
    this.code = frame.code;
    this.data = frame.data;
  }
}

// -- Server-defined business errors (specs §4) -------------------------------

export class SessionNotFoundError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'SessionNotFoundError';
  }
}
export class SessionLockedError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'SessionLockedError';
  }
}
export class TurnInProgressError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'TurnInProgressError';
  }
}
export class McpServerNotConnectedError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'McpServerNotConnectedError';
  }
}
export class McpToolCallFailedError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'McpToolCallFailedError';
  }
}
export class SkillNotFoundError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'SkillNotFoundError';
  }
}
export class SkillPinConflictError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'SkillPinConflictError';
  }
}
export class ModelNotAvailableError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'ModelNotAvailableError';
  }
}
export class ModelSwitchInTurnError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'ModelSwitchInTurnError';
  }
}
export class ConfigFieldReadonlyError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'ConfigFieldReadonlyError';
  }
}
export class ConfigValidationError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'ConfigValidationError';
  }
}
export class NotSupportedInV01Error extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'NotSupportedInV01Error';
  }
}
export class CliCommandFailedError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'CliCommandFailedError';
  }
}
export class CliCommandTimeoutError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'CliCommandTimeoutError';
  }
}
export class NotDispatchCompatibleError extends RpcError {
  constructor(f: JsonRpcErrorObject) {
    super(f);
    this.name = 'NotDispatchCompatibleError';
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
  [-32015]: NotDispatchCompatibleError,
};

/** Pick the right subclass for an incoming JSON-RPC error frame. */
export function rpcErrorFromFrame(frame: JsonRpcErrorObject): RpcError {
  const Ctor = CODE_TO_CTOR[frame.code];
  return Ctor ? new Ctor(frame) : new RpcError(frame);
}
