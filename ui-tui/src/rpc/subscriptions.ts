// Raven TUI RPC — server-push subscription registry.
//
// Server sends `method: "event"` JSON-RPC notification frames (specs §2.4)
// with `params: { subscription_id, event }`. This registry maps
// subscription_id → handler so `RpcClient` can route each frame to the right
// consumer.
//
// Unknown subscription_ids do not crash the client — we just log a warning
// to stderr and drop the frame.

import type { EventNotificationParams } from './generated.js'

type AnyHandler = (event: unknown) => void

export class SubscriptionRegistry {
  private readonly handlers = new Map<string, AnyHandler>()

  /** Register a handler for a subscription_id returned by the server. */
  register<E>(subscriptionId: string, handler: (event: E) => void): void {
    this.handlers.set(subscriptionId, handler as AnyHandler)
  }

  /** Remove a handler. Subsequent notifications for this id are dropped. */
  unregister(subscriptionId: string): boolean {
    return this.handlers.delete(subscriptionId)
  }

  /** True if a handler is currently registered for this id. */
  has(subscriptionId: string): boolean {
    return this.handlers.has(subscriptionId)
  }

  /** Number of active subscriptions (mainly useful in tests). */
  size(): number {
    return this.handlers.size
  }

  /**
   * Dispatch an incoming event notification to the registered handler.
   * Returns true if a handler was invoked, false otherwise.
   * Unknown subscription_ids produce a stderr warn but never throw.
   */
  dispatch(params: EventNotificationParams<unknown>): boolean {
    const handler = this.handlers.get(params.subscription_id)
    if (!handler) {
      process.stderr.write(`[rpc-subscriptions] event for unknown subscription_id=${params.subscription_id} dropped\n`)
      return false
    }
    try {
      handler(params.event)
    } catch (err) {
      // Handler crashes must not poison the read loop.
      process.stderr.write(`[rpc-subscriptions] handler for ${params.subscription_id} threw: ${String(err)}\n`)
    }
    return true
  }

  /** Drop every registration. Used by `RpcClient.close()`. */
  clear(): void {
    this.handlers.clear()
  }
}
