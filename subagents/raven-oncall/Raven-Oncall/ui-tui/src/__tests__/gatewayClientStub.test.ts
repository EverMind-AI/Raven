// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { describe, expect, it, vi } from 'vitest'

import type {
  CommandsCatalogResponse,
  GatewayEvent,
  SessionListResponse,
  SessionResumeResponse,
  SetupStatusResponse
} from '../gatewayTypes.js'

import { GatewayClientStub } from '../gatewayClientStub.js'

describe('GatewayClientStub', () => {
  it('start() does not spawn subprocess or open sockets, completes synchronously', () => {
    const c = new GatewayClientStub()
    expect(() => c.start()).not.toThrow()
    c.kill()
  })

  it('start() then drain() emits a gateway.ready event', async () => {
    const c = new GatewayClientStub()
    const events: GatewayEvent[] = []
    c.on('event', (ev: GatewayEvent) => events.push(ev))

    c.start()
    // gateway.ready is buffered before drain() and flushed on drain()
    c.drain()
    // Allow the ready timer's setTimeout(0) to fire
    await new Promise(resolve => setTimeout(resolve, 60))

    expect(events.some(e => e.type === 'gateway.ready')).toBe(true)
  })

  it('request(session.list) returns one mock session', async () => {
    const c = new GatewayClientStub()
    c.start()
    const r = await c.request<SessionListResponse>('session.list', {})
    expect(r.sessions).toBeDefined()
    expect(r.sessions).toHaveLength(1)
    expect(r.sessions![0].id).toBe('mock-session-1')
    c.kill()
  })

  it('request(session.resume) returns 3 messages with roles [system, user, assistant]', async () => {
    const c = new GatewayClientStub()
    c.start()

    const r = await c.request<SessionResumeResponse>('session.resume', {
      cols: 80,
      session_id: 'mock-session-1'
    })

    expect(r.messages).toHaveLength(3)
    expect(r.messages.map(m => m.role)).toEqual(['system', 'user', 'assistant'])
    c.kill()
  })

  it('request(commands.catalog) returns at least one slash command category', async () => {
    const c = new GatewayClientStub()
    c.start()
    const r = await c.request<CommandsCatalogResponse>('commands.catalog', {})
    expect(r.categories).toBeDefined()
    expect(r.categories!.length).toBeGreaterThan(0)
    c.kill()
  })

  it('request(setup.status) reports provider_configured=true (skips setup wizard)', async () => {
    const c = new GatewayClientStub()
    c.start()
    const r = await c.request<SetupStatusResponse>('setup.status', {})
    expect(r.provider_configured).toBe(true)
    c.kill()
  })

  it('request(unknown.method) resolves to empty object and logs warning (does not reject)', async () => {
    const c = new GatewayClientStub()
    c.start()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    const r = await c.request('totally.made.up', { foo: 1 })

    expect(r).toEqual({})
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('totally.made.up'))

    warn.mockRestore()
    c.kill()
  })

  it('kill() does not throw; subsequent start() works', () => {
    const c = new GatewayClientStub()
    c.start()
    expect(() => c.kill()).not.toThrow()
    expect(() => c.start()).not.toThrow()
    c.kill()
  })

  it('getLogTail(limit) returns a string', () => {
    const c = new GatewayClientStub()
    c.start()
    expect(typeof c.getLogTail(10)).toBe('string')
    c.kill()
  })
})
