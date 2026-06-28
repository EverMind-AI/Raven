// Raven TUI RPC — subscription registry + end-to-end push-event routing.

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { createServer } from 'node:net';
import type { Server, Socket } from 'node:net';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { RpcClient } from '../client.js';
import { SubscriptionRegistry } from '../subscriptions.js';
import type { TurnEvent, TurnSubscribeResult } from '../generated.js';

// ---- Unit tests on SubscriptionRegistry (no socket) -----------------------

describe('SubscriptionRegistry', () => {
  it('routes events to the registered handler', () => {
    const r = new SubscriptionRegistry();
    const received: unknown[] = [];
    r.register('sub-1', (e: unknown) => received.push(e));
    r.dispatch({ subscription_id: 'sub-1', event: { type: 'token.delta', payload: { text: 'a' } } });
    r.dispatch({ subscription_id: 'sub-1', event: { type: 'token.delta', payload: { text: 'b' } } });
    expect(received).toHaveLength(2);
  });

  it('isolates handlers across subscription_ids', () => {
    const r = new SubscriptionRegistry();
    const a: unknown[] = [];
    const b: unknown[] = [];
    r.register('a', (e: unknown) => a.push(e));
    r.register('b', (e: unknown) => b.push(e));
    r.dispatch({ subscription_id: 'a', event: 1 });
    r.dispatch({ subscription_id: 'b', event: 2 });
    r.dispatch({ subscription_id: 'a', event: 3 });
    expect(a).toEqual([1, 3]);
    expect(b).toEqual([2]);
  });

  it('drops events for unknown subscription_ids without throwing', () => {
    const r = new SubscriptionRegistry();
    expect(() =>
      r.dispatch({ subscription_id: 'ghost', event: { type: 'noop' } }),
    ).not.toThrow();
  });

  it('unregister stops further dispatches to that handler', () => {
    const r = new SubscriptionRegistry();
    const seen: unknown[] = [];
    r.register('s', (e: unknown) => seen.push(e));
    r.dispatch({ subscription_id: 's', event: 1 });
    expect(r.unregister('s')).toBe(true);
    r.dispatch({ subscription_id: 's', event: 2 });
    expect(seen).toEqual([1]);
  });

  it('survives handler exceptions', () => {
    const r = new SubscriptionRegistry();
    r.register('s', () => {
      throw new Error('boom');
    });
    expect(() => r.dispatch({ subscription_id: 's', event: 1 })).not.toThrow();
  });
});

// ---- End-to-end: client.subscribe routes server push events ---------------

interface MockServer {
  socketPath: string;
  server: Server;
  send: (frame: Record<string, unknown>) => void;
  setHandler: (h: (frame: Record<string, unknown>) => void) => void;
  close: () => Promise<void>;
}

function startMock(): Promise<MockServer> {
  return new Promise((resolve, reject) => {
    const dir = mkdtempSync(join(tmpdir(), 'eve-rpc-sub-'));
    const socketPath = join(dir, 'sock');
    let activeSocket: Socket | null = null;
    let handler: (frame: Record<string, unknown>) => void = () => {};
    let readBuf = '';
    const server = createServer((socket) => {
      activeSocket = socket;
      socket.setEncoding('utf-8');
      socket.on('data', (chunk: string | Buffer) => {
        readBuf += typeof chunk === 'string' ? chunk : chunk.toString('utf-8');
        let nl = readBuf.indexOf('\n');
        while (nl !== -1) {
          const line = readBuf.slice(0, nl).trim();
          readBuf = readBuf.slice(nl + 1);
          if (line.length > 0) {
            try {
              handler(JSON.parse(line));
            } catch {
              /* swallow */
            }
          }
          nl = readBuf.indexOf('\n');
        }
      });
      socket.on('close', () => {
        activeSocket = null;
      });
    });
    server.listen(socketPath, () => {
      resolve({
        socketPath,
        server,
        send: (frame) => {
          if (!activeSocket) throw new Error('no active connection');
          activeSocket.write(JSON.stringify(frame) + '\n');
        },
        setHandler: (h) => {
          handler = h;
        },
        close: () =>
          new Promise<void>((res) => {
            if (activeSocket) activeSocket.destroy();
            server.close(() => {
              rmSync(dir, { recursive: true, force: true });
              res();
            });
          }),
      });
    });
    server.on('error', reject);
  });
}

describe('RpcClient.subscribe', () => {
  let mock: MockServer;
  let client: RpcClient;

  beforeEach(async () => {
    mock = await startMock();
  });
  afterEach(async () => {
    if (client) client.close();
    await mock.close();
  });

  it('routes 100 token.delta notifications to the registered handler', async () => {
    mock.setHandler((frame) => {
      if (frame.method === 'turn.subscribe') {
        const result: TurnSubscribeResult = { subscription_id: 'sub-99' };
        mock.send({ jsonrpc: '2.0', id: frame.id as number, result });
      }
    });
    client = new RpcClient({ socketPath: mock.socketPath });
    const received: TurnEvent[] = [];
    const { subscription_id } = await client.subscribe<TurnEvent>(
      'turn.subscribe',
      { session_key: 'tui:default' },
      (event) => received.push(event),
    );
    expect(subscription_id).toBe('sub-99');

    for (let i = 0; i < 100; i++) {
      mock.send({
        jsonrpc: '2.0',
        method: 'event',
        params: {
          subscription_id: 'sub-99',
          event: { type: 'token.delta', payload: { text: `chunk-${i}` } },
        },
      });
    }
    // Allow drain
    await new Promise((r) => setTimeout(r, 50));
    expect(received).toHaveLength(100);
    expect(received[0]).toEqual({ type: 'token.delta', payload: { text: 'chunk-0' } });
    expect(received[99]).toEqual({ type: 'token.delta', payload: { text: 'chunk-99' } });

    // TurnEvent narrowing sanity (compile-time): if the type is broken, this
    // block would fail to typecheck.
    const last = received[99];
    if (last.type === 'token.delta') {
      expect(typeof last.payload.text).toBe('string');
    }
  });

  it('unsubscribe removes the handler — subsequent events are ignored', async () => {
    mock.setHandler((frame) => {
      if (frame.method === 'turn.subscribe') {
        mock.send({
          jsonrpc: '2.0',
          id: frame.id as number,
          result: { subscription_id: 'sub-1' } as TurnSubscribeResult,
        });
      } else if (frame.method === 'turn.unsubscribe') {
        mock.send({
          jsonrpc: '2.0',
          id: frame.id as number,
          result: { unsubscribed: true },
        });
      }
    });
    client = new RpcClient({ socketPath: mock.socketPath });
    const received: TurnEvent[] = [];
    const { unsubscribe } = await client.subscribe<TurnEvent>(
      'turn.subscribe',
      { session_key: 'tui:default' },
      (e) => received.push(e),
      { unsubscribeMethod: 'turn.unsubscribe' },
    );
    mock.send({
      jsonrpc: '2.0',
      method: 'event',
      params: { subscription_id: 'sub-1', event: { type: 'token.delta', payload: { text: 'a' } } },
    });
    await new Promise((r) => setTimeout(r, 20));
    await unsubscribe();
    mock.send({
      jsonrpc: '2.0',
      method: 'event',
      params: { subscription_id: 'sub-1', event: { type: 'token.delta', payload: { text: 'b' } } },
    });
    await new Promise((r) => setTimeout(r, 20));
    expect(received).toHaveLength(1);
    expect(client.subscriptionCount()).toBe(0);
  });

  it('does not cross-contaminate multiple parallel subscriptions', async () => {
    let nextSubId = 1;
    mock.setHandler((frame) => {
      if (frame.method === 'turn.subscribe') {
        mock.send({
          jsonrpc: '2.0',
          id: frame.id as number,
          result: { subscription_id: `sub-${nextSubId++}` } as TurnSubscribeResult,
        });
      }
    });
    client = new RpcClient({ socketPath: mock.socketPath });
    const aBucket: TurnEvent[] = [];
    const bBucket: TurnEvent[] = [];
    const a = await client.subscribe<TurnEvent>(
      'turn.subscribe',
      { session_key: 'tui:a' },
      (e) => aBucket.push(e),
    );
    const b = await client.subscribe<TurnEvent>(
      'turn.subscribe',
      { session_key: 'tui:b' },
      (e) => bBucket.push(e),
    );
    expect(a.subscription_id).not.toBe(b.subscription_id);

    mock.send({
      jsonrpc: '2.0',
      method: 'event',
      params: { subscription_id: a.subscription_id, event: { type: 'token.delta', payload: { text: 'A' } } },
    });
    mock.send({
      jsonrpc: '2.0',
      method: 'event',
      params: { subscription_id: b.subscription_id, event: { type: 'token.delta', payload: { text: 'B' } } },
    });
    await new Promise((r) => setTimeout(r, 30));
    expect(aBucket).toHaveLength(1);
    expect(bBucket).toHaveLength(1);
    expect(client.subscriptionCount()).toBe(2);
  });
});
