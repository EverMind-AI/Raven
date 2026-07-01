// Raven TUI RPC — one-stop public entrypoint for consumers.
//
// ui-tui consumers should import everything from `./rpc`:
//   import { RpcClient, SessionNotFoundError, type TurnEvent } from './rpc';

export { RpcClient } from './client.js'
export type { RpcClientOptions } from './client.js'
export { SubscriptionRegistry } from './subscriptions.js'
export {
  RpcError,
  SessionNotFoundError,
  SessionLockedError,
  TurnInProgressError,
  McpServerNotConnectedError,
  McpToolCallFailedError,
  SkillNotFoundError,
  SkillPinConflictError,
  ModelNotAvailableError,
  ModelSwitchInTurnError,
  ConfigFieldReadonlyError,
  ConfigValidationError,
  NotSupportedInV01Error,
  CliCommandFailedError,
  CliCommandTimeoutError,
  NotDispatchCompatibleError,
  rpcErrorFromFrame
} from './errors.js'
export * from './generated.js'
