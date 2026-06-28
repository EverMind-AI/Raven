// Raven TUI RPC — production JSON-RPC 2.0 client.
//
// Transport: unix domain socket (per Q11 decision — see
// `docs/RepoMem/temp/tui-ipc-bridge/11-tui-commands-transport-decision.md`).
// The Python parent listens; the Node child connects via `net.createConnection`.
// Bare FD inheritance (pass_fds=(3,4)) was rejected — Node has no stable way
// to wrap inherited pipe FDs as streams when running as the child process.
//
// Framing: newline-delimited UTF-8 JSON (specs §2.5). Each frame is
// `JSON.stringify(obj) + '\n'`. Single frame limit: 1 MiB.
//
// Writes are serialized through a `writeQueue` (single-writer model) so
// concurrent `rpc()` / `subscribe()` calls never interleave bytes.
//
// Error mapping: incoming JSON-RPC error frames are converted to typed
// `RpcError` subclasses via `errors.ts::rpcErrorFromFrame`.

import { createConnection } from 'node:net';
import type { Socket } from 'node:net';
import type {
  EventNotificationParams,
  JsonRpcErrorResponse,
  JsonRpcRequest,
  JsonRpcResponse,
} from './generated.js';
import { isJsonRpcError } from './generated.js';
import { rpcErrorFromFrame } from './errors.js';
import { SubscriptionRegistry } from './subscriptions.js';

const MAX_FRAME_BYTES = 1024 * 1024; // 1 MiB (specs §2.5)

type Pending = {
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
};

export interface RpcClientOptions {
  /** Unix socket path. Defaults to env `RAVEN_RPC_SOCKET`. */
  socketPath?: string;
  /** Optional logger for non-fatal protocol oddities. Defaults to stderr. */
  warn?: (msg: string) => void;
  /**
   * Sink for server-initiated notifications whose method is NOT `event`
   * (the subscription-stream envelope). The confirm round-trip
   * (`confirm.request`) arrives this way — a first-class top-level method,
   * not a per-subscription stream event. When omitted, such notifications
   * are logged as unknown and dropped.
   */
  onNotification?: (method: string, params: unknown) => void;
}

export class RpcClient {
  private readonly socket: Socket;
  private readonly registry = new SubscriptionRegistry();
  private readonly pending = new Map<number, Pending>();
  private readonly warn: (msg: string) => void;
  private readonly onNotification?: (method: string, params: unknown) => void;

  private nextId = 1;
  private readBuffer = '';
  private writeQueue: Promise<void> = Promise.resolve();
  private closed = false;
  private connected = false;
  private readonly connectPromise: Promise<void>;

  constructor(opts: RpcClientOptions = {}) {
    const socketPath = opts.socketPath ?? process.env.RAVEN_RPC_SOCKET;
    if (!socketPath) {
      throw new Error(
        'RpcClient: no socket path supplied; pass `socketPath` or set ' +
          'RAVEN_RPC_SOCKET env var.',
      );
    }
    this.warn = opts.warn ?? ((m) => process.stderr.write(`[rpc-client] ${m}\n`));
    this.onNotification = opts.onNotification;

    this.socket = createConnection(socketPath);
    this.socket.setEncoding('utf-8');

    this.connectPromise = new Promise<void>((resolve, reject) => {
      const onConnect = () => {
        this.connected = true;
        this.socket.off('error', onError);
        resolve();
      };
      const onError = (err: Error) => {
        this.socket.off('connect', onConnect);
        reject(err);
      };
      this.socket.once('connect', onConnect);
      this.socket.once('error', onError);
    });

    this.socket.on('data', (chunk: string | Buffer) => {
      const text = typeof chunk === 'string' ? chunk : chunk.toString('utf-8');
      this.readBuffer += text;
      if (this.readBuffer.length > MAX_FRAME_BYTES * 2) {
        // Defensive: if peer is flooding without newlines, abort rather than OOM.
        this.warn(
          `incoming read buffer exceeded ${MAX_FRAME_BYTES * 2} bytes without ` +
            'newline — closing connection',
        );
        this.failAll(new Error('rpc-client: frame size limit exceeded'));
        this.socket.destroy();
        return;
      }
      this.drainBuffer();
    });
    this.socket.on('end', () => this.failAll(new Error('socket closed by peer')));
    this.socket.on('error', (err) => this.failAll(err));
  }

  /** Awaitable handle that resolves once the socket connection is established. */
  ready(): Promise<void> {
    return this.connectPromise;
  }

  private drainBuffer(): void {
    let nl = this.readBuffer.indexOf('\n');
    while (nl !== -1) {
      const line = this.readBuffer.slice(0, nl).trim();
      this.readBuffer = this.readBuffer.slice(nl + 1);
      if (line.length > 0) this.handleFrame(line);
      nl = this.readBuffer.indexOf('\n');
    }
  }

  private handleFrame(line: string): void {
    if (line.length > MAX_FRAME_BYTES) {
      this.warn(`oversized frame (${line.length} bytes) dropped`);
      return;
    }
    let frame: unknown;
    try {
      frame = JSON.parse(line);
    } catch {
      this.warn(`malformed frame ignored: ${line.slice(0, 120)}`);
      return;
    }
    if (!frame || typeof frame !== 'object') {
      this.warn('non-object frame ignored');
      return;
    }
    const obj = frame as Record<string, unknown>;

    // Notification frame (no `id`, has `method`)
    if (obj.id === undefined && typeof obj.method === 'string') {
      if (obj.method === 'event') {
        const params = obj.params as EventNotificationParams<unknown> | undefined;
        if (params && typeof params.subscription_id === 'string') {
          this.registry.dispatch(params);
        } else {
          this.warn('event notification missing subscription_id/event');
        }
      } else if (this.onNotification) {
        // First-class top-level notifications (e.g. confirm.request) are not
        // subscription-stream events; hand them to the consumer's sink.
        this.onNotification(obj.method, obj.params);
      } else {
        this.warn(`unknown notification method: ${obj.method}`);
      }
      return;
    }

    // Response frame (has `id`)
    const resp = frame as JsonRpcResponse<unknown>;
    const id = resp.id;
    if (typeof id !== 'number' && typeof id !== 'string') {
      this.warn('response frame has no valid id');
      return;
    }
    const idKey = typeof id === 'number' ? id : Number(id);
    const pending = this.pending.get(idKey);
    if (!pending) {
      this.warn(`response for unknown id ${String(id)}`);
      return;
    }
    this.pending.delete(idKey);
    if (isJsonRpcError(resp)) {
      pending.reject(rpcErrorFromFrame((resp as JsonRpcErrorResponse).error));
    } else {
      pending.resolve(resp.result);
    }
  }

  private failAll(err: Error): void {
    if (this.closed) return;
    this.closed = true;
    for (const [, p] of this.pending) p.reject(err);
    this.pending.clear();
    this.registry.clear();
  }

  private async writeFrame(frame: string): Promise<void> {
    if (frame.length > MAX_FRAME_BYTES) {
      throw new Error(
        `rpc-client: outgoing frame ${frame.length} bytes exceeds ${MAX_FRAME_BYTES} limit`,
      );
    }
    // Serialize all writes — even when the socket itself is happy with
    // concurrent writes, we don't want two frames interleaved on the wire.
    const prev = this.writeQueue;
    this.writeQueue = (async () => {
      await prev;
      if (!this.connected) await this.connectPromise;
      await new Promise<void>((resolve, reject) => {
        this.socket.write(frame, (err) => (err ? reject(err) : resolve()));
      });
    })();
    return this.writeQueue;
  }

  /** Invoke a JSON-RPC method and await the typed result. */
  async rpc<R = unknown, P = unknown>(method: string, params: P): Promise<R> {
    if (this.closed) throw new Error('rpc-client: closed');
    const id = this.nextId++;
    const req: JsonRpcRequest<P> = { jsonrpc: '2.0', id, method, params };
    const frame = JSON.stringify(req) + '\n';
    const result = new Promise<R>((resolve, reject) => {
      this.pending.set(id, {
        resolve: (v) => resolve(v as R),
        reject,
      });
    });
    try {
      await this.writeFrame(frame);
    } catch (err) {
      this.pending.delete(id);
      throw err;
    }
    return result;
  }

  /**
   * Subscribe to a server-push stream (e.g. `turn.subscribe`).
   *
   * The server returns a `{subscription_id}` result; this method registers
   * the handler against that id and returns an `unsubscribe()` thunk that
   * both calls the paired server method (if `unsubscribeMethod` is given)
   * and detaches the handler locally.
   */
  async subscribe<E = unknown, P = unknown, R extends { subscription_id: string } = { subscription_id: string }>(
    method: string,
    params: P,
    handler: (event: E) => void,
    opts: { unsubscribeMethod?: string } = {},
  ): Promise<{ subscription_id: string; unsubscribe: () => Promise<void> }> {
    const result = await this.rpc<R, P>(method, params);
    const subscriptionId = result.subscription_id;
    this.registry.register<E>(subscriptionId, handler);
    const unsubscribeMethod = opts.unsubscribeMethod;
    const unsubscribe = async (): Promise<void> => {
      this.registry.unregister(subscriptionId);
      if (unsubscribeMethod && !this.closed) {
        await this.rpc<unknown, { subscription_id: string }>(unsubscribeMethod, {
          subscription_id: subscriptionId,
        });
      }
    };
    return { subscription_id: subscriptionId, unsubscribe };
  }

  /** Number of pending requests (mainly for tests). */
  pendingCount(): number {
    return this.pending.size;
  }

  /** Number of active subscriptions (mainly for tests). */
  subscriptionCount(): number {
    return this.registry.size();
  }

  /** Tear down the socket and reject every pending promise. */
  close(): void {
    this.failAll(new Error('rpc-client: closed by caller'));
    try {
      this.socket.end();
    } catch {
      /* noop */
    }
    try {
      this.socket.destroy();
    } catch {
      /* noop */
    }
  }
}
