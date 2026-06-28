// Raven TUI RPC — client.ts integration tests against a mock unix socket
// server. Covers happy path, parallel requests, typed error mapping, abrupt
// disconnect, frame size enforcement, and codegen type-sanity.

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { createServer } from 'node:net';
import type { Server, Socket } from 'node:net';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { RpcClient } from '../client.js';
import {
  SessionNotFoundError,
  ConfigValidationError,
  RpcError,
} from '../errors.js';
import type {
  CliDispatchParams,
  CliDispatchResult,
  SystemHelloResult,
} from '../generated.js';

// --------------------------------------------------------------------------
// Mock server: line-delimited JSON, handler-driven. Each connection's frames
// flow into `onFrame`, which can write 0+ response frames back.
// --------------------------------------------------------------------------

type Frame = Record<string, unknown>;

interface MockServer {
  socketPath: string;
  server: Server;
  /** Push response/notification frames into the currently-connected client. */
  send: (frame: Frame) => void;
  /** Replace the per-frame handler. */
  setHandler: (h: (frame: Frame) => void) => void;
  /** Force-close the active connection (simulates server crash). */
  killConnection: () => void;
  close: () => Promise<void>;
}

function startMock(): Promise<MockServer> {
  return new Promise((resolve, reject) => {
    const dir = mkdtempSync(join(tmpdir(), 'eve-rpc-test-'));
    const socketPath = join(dir, 'sock');
    let activeSocket: Socket | null = null;
    let handler: (frame: Frame) => void = () => {};
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
            } catch (e) {
              // swallow — let tests assert on outcome
              void e;
            }
          }
          nl = readBuf.indexOf('\n');
        }
      });
      socket.on('error', () => {
        activeSocket = null;
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
        killConnection: () => {
          if (activeSocket) {
            activeSocket.destroy();
            activeSocket = null;
          }
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

// --------------------------------------------------------------------------
// Tests
// --------------------------------------------------------------------------

describe('RpcClient', () => {
  let mock: MockServer;
  let client: RpcClient;

  beforeEach(async () => {
    mock = await startMock();
  });
  afterEach(async () => {
    if (client) client.close();
    await mock.close();
  });

  it('completes a happy-path request (system.hello echo)', async () => {
    mock.setHandler((frame) => {
      expect(frame.method).toBe('system.hello');
      expect(frame.jsonrpc).toBe('2.0');
      mock.send({
        jsonrpc: '2.0',
        id: frame.id as number,
        result: {
          server_version: '0.1.0',
          server_capabilities: ['cli.dispatch'],
          session: { default_channel: 'tui', default_session_key: 'tui:default' },
        },
      });
    });
    client = new RpcClient({ socketPath: mock.socketPath });
    const result = await client.rpc<SystemHelloResult>('system.hello', {
      client_version: '0.0.1',
    });
    expect(result.server_version).toBe('0.1.0');
    expect(result.session.default_channel).toBe('tui');
  });

  it('routes parallel responses to the correct caller by id', async () => {
    // Buffer incoming requests and reply out-of-order to verify id-matching.
    const seen: Frame[] = [];
    mock.setHandler((frame) => {
      seen.push(frame);
      if (seen.length === 3) {
        // Reply in reverse order
        for (let i = 2; i >= 0; i--) {
          mock.send({
            jsonrpc: '2.0',
            id: seen[i].id as number,
            result: { pong: true, server_time_ms: (i + 1) * 100 },
          });
        }
      }
    });
    client = new RpcClient({ socketPath: mock.socketPath });
    const [a, b, c] = await Promise.all([
      client.rpc<{ server_time_ms: number }>('system.ping', {}),
      client.rpc<{ server_time_ms: number }>('system.ping', {}),
      client.rpc<{ server_time_ms: number }>('system.ping', {}),
    ]);
    expect(a.server_time_ms).toBe(100);
    expect(b.server_time_ms).toBe(200);
    expect(c.server_time_ms).toBe(300);
  });

  it('maps -32001 errors to SessionNotFoundError', async () => {
    mock.setHandler((frame) => {
      mock.send({
        jsonrpc: '2.0',
        id: frame.id as number,
        error: {
          code: -32001,
          message: 'session_not_found',
          data: { session_key: 'cli:nope' },
        },
      });
    });
    client = new RpcClient({ socketPath: mock.socketPath });
    await expect(client.rpc('session.get', { session_key: 'cli:nope' })).rejects.toBeInstanceOf(
      SessionNotFoundError,
    );
  });

  it('maps -32011 to ConfigValidationError and preserves code+data', async () => {
    mock.setHandler((frame) => {
      mock.send({
        jsonrpc: '2.0',
        id: frame.id as number,
        error: { code: -32011, message: 'config_validation_error', data: { field: 'channel' } },
      });
    });
    client = new RpcClient({ socketPath: mock.socketPath });
    try {
      await client.rpc('session.create', { channel: '', chat_id: 'x' });
      throw new Error('should have rejected');
    } catch (err) {
      expect(err).toBeInstanceOf(ConfigValidationError);
      expect(err).toBeInstanceOf(RpcError);
      const re = err as RpcError;
      expect(re.code).toBe(-32011);
      expect((re.data as { field: string }).field).toBe('channel');
    }
  });

  it('rejects all pending promises when peer disconnects mid-flight', async () => {
    // Capture the request but never reply — then drop the connection.
    mock.setHandler(() => {
      setTimeout(() => mock.killConnection(), 10);
    });
    client = new RpcClient({ socketPath: mock.socketPath });
    const p1 = client.rpc('system.ping', {});
    const p2 = client.rpc('system.ping', {});
    await expect(p1).rejects.toThrow(/socket closed|rpc-client/);
    await expect(p2).rejects.toThrow(/socket closed|rpc-client/);
  });

  it('refuses to write frames over 1 MiB', async () => {
    mock.setHandler(() => {
      // never reached
    });
    client = new RpcClient({ socketPath: mock.socketPath });
    await client.ready();
    const huge = 'x'.repeat(1024 * 1024 + 100); // > 1 MiB after JSON-stringify
    await expect(
      client.rpc('cli.dispatch', { argv: [huge], width: 80 } as CliDispatchParams),
    ).rejects.toThrow(/frame.*exceeds/);
  });

  it('typecheck: CliDispatchParams + CliDispatchResult are reachable from generated.ts', async () => {
    // This test exists primarily for the compile-time `tsc` pass — if the
    // codegen broke, the import or the field access would fail to compile.
    mock.setHandler((frame) => {
      const params = frame.params as CliDispatchParams;
      expect(Array.isArray(params.argv)).toBe(true);
      expect(typeof params.width).toBe('number');
      const result: CliDispatchResult = {
        stdout: 'ok\n',
        stderr: '',
        exit_code: 0,
      };
      mock.send({ jsonrpc: '2.0', id: frame.id as number, result });
    });
    client = new RpcClient({ socketPath: mock.socketPath });
    const out = await client.rpc<CliDispatchResult, CliDispatchParams>('cli.dispatch', {
      argv: ['version'],
      width: 80,
    });
    expect(out.exit_code).toBe(0);
    expect(out.stdout).toContain('ok');
  });
});
