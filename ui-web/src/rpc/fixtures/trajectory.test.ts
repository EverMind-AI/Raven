import { afterEach, describe, expect, it } from 'vitest'

import { resetCapabilities, servesTrajectory } from '../capabilities'
import { FixtureTransport } from '../fixtureTransport'
import { demoFixtures } from './index'

afterEach(() => {
  resetCapabilities()
})

const transport = () => new FixtureTransport(demoFixtures, { now: () => 1789000000000, timer: (_ms, fn) => fn() })

describe('the offline trajectory library', () => {
  it('is announced by the offline handshake, so the entry can appear', async () => {
    expect(servesTrajectory()).toBe(false)
    const hello = await transport().call('system.hello', { client_version: '0.1.0' })
    expect(hello.server_capabilities).toContain('trajectory-v1')
    expect(servesTrajectory()).toBe(true)
    const state = await transport().call('trajectory.state', {})
    expect(state.enabled).toBe(true)
  })

  it('answers one recorded turn with the shape the page reads', async () => {
    const page = await transport().call('trajectory.list', { session_key: 'gui:any' })
    expect(page.complete).toBe(true)
    expect(page.index_state.phase).toBe('ready')
    expect(page.entries).toHaveLength(8)
    expect(new Set(page.entries.map((e) => e.entry_id)).size).toBe(8)
    expect(page.entries[0]?.turn_start).toBe(true)
    expect(page.entries.filter((e) => e.failure_entry).map((e) => e.entry_id)).toEqual(['t:tool1:tool.output'])
    const charged = page.entries.map((e) => e.charged_ms ?? 0).reduce((a, b) => a + b, 0)
    expect(charged).toBe(3200 + 600 + 800 + 12000)
    const twice = await transport().call('trajectory.list', { session_key: 'gui:other' })
    expect(twice).toEqual(page)
  })

  it('follows the snapshot with an empty change feed and resets on a foreign epoch', async () => {
    const page = await transport().call('trajectory.list', { session_key: 'gui:any' })
    const same = await transport().call('trajectory.changes', { session_key: 'gui:any', epoch: page.epoch, after_revision: page.snapshot_revision })
    expect(same.upserts).toEqual([])
    expect(same.reset_required).toBe(false)
    const foreign = await transport().call('trajectory.changes', { session_key: 'gui:any', epoch: 'elsewhere', after_revision: 0 })
    expect(foreign.reset_required).toBe(true)
  })

  it('describes every listed entry and serves each of its blocks', async () => {
    const t = transport()
    const page = await t.call('trajectory.list', { session_key: 'gui:any' })
    for (const row of page.entries) {
      const detail = await t.call('trajectory.detail', { session_key: 'gui:any', entry_id: row.entry_id })
      expect(detail.entry_revision).toBe(row.revision)
      const ids = detail.blocks.map((b) => b.id)
      expect(new Set(ids).size).toBe(ids.length)
      expect(ids.slice(-3)).toEqual(['timing', 'relations', 'raw'])
      for (const block of detail.blocks) {
        const body = await t.call('trajectory.block', {
          session_key: 'gui:any', entry_id: row.entry_id, entry_revision: row.revision, epoch: page.epoch, block_id: block.id,
        })
        expect(body.renderer).toBe(block.renderer)
        if (block.availability === 'available') expect(body.data).not.toBeNull()
      }
    }
    const unknown = await t.call('trajectory.detail', { session_key: 'gui:any', entry_id: 'nope' })
    expect(unknown.entry_id).toBe(page.entries[0]?.entry_id)
  })

  it('keeps each tool result on its own call, parameters and outcome', async () => {
    const t = transport()
    const page = await t.call('trajectory.list', { session_key: 'gui:any' })
    const read = async (entryId: string, blockId: string) => {
      const row = page.entries.find((e) => e.entry_id === entryId)!
      const body = await t.call('trajectory.block', {
        session_key: 'gui:any', entry_id: entryId, entry_revision: row.revision, epoch: page.epoch, block_id: blockId,
      })
      return body.data as Record<string, unknown>
    }
    const failed = await t.call('trajectory.detail', { session_key: 'gui:any', entry_id: 't:tool1:tool.output' })
    expect(failed.operation_status).toBe('error')
    expect(failed.blocks.map((b) => b.id)).toContain('error')
    expect(failed.blocks.map((b) => b.id)).not.toContain('tool')
    expect(page.entries.find((e) => e.entry_id === 't:tool1:tool.output')?.meta).toEqual({ tool: 'list_issues' })
    expect(failed.blocks.find((b) => b.id === 'params')?.preview).toEqual({ state: 'open' })
    expect((await read('t:tool1:tool.output', 'result')).text).toMatch(/unreachable/)
    expect((await read('t:tool1:tool.output', 'params')).value).toEqual({ state: 'open' })

    const ok = await t.call('trajectory.detail', { session_key: 'gui:any', entry_id: 't:tool2:tool.output' })
    expect(ok.operation_status).toBe('ok')
    expect(ok.blocks.map((b) => b.id)).not.toContain('error')
    expect(page.entries.find((e) => e.entry_id === 't:tool2:tool.output')?.meta).toEqual({ tool: 'read_issue' })
    expect(ok.blocks.find((b) => b.id === 'params')?.preview).toEqual({ id: 42 })
    const result = await read('t:tool2:tool.output', 'result')
    expect(result.text).toMatch(/^# Issue 42: flaky retry on the sync path\n/)
    expect(result.text).not.toMatch(/unreachable/)
    expect((await read('t:tool2:tool.output', 'params')).value).toEqual({ id: 42 })
  })

  it('keeps a finished entry at its times while the clock moves on', async () => {
    let now = 1789000000000
    const t = new FixtureTransport(demoFixtures, { now: () => now, timer: (_ms, fn) => fn() })
    const first = await t.call('trajectory.list', { session_key: 'gui:any' })
    now += 60_000
    const later = await t.call('trajectory.list', { session_key: 'gui:any' })
    expect(later).toEqual(first)
    const row = first.entries[0]!
    const timing = await t.call('trajectory.block', {
      session_key: 'gui:any', entry_id: row.entry_id, entry_revision: row.revision, epoch: first.epoch, block_id: 'timing',
    })
    const items = (timing.data as { items: Array<{ key: string; value: unknown }> }).items
    expect(items.find((i) => i.key === 'event_time')?.value).toBe(row.event_time)
    expect(items.find((i) => i.key === 'operation_start')?.value).toBe(row.operation_start)
    expect(items.find((i) => i.key === 'operation_end')?.value).toBe(row.operation_end)
    const feed = await t.call('trajectory.changes', { session_key: 'gui:any', epoch: first.epoch, after_revision: first.snapshot_revision })
    expect(feed.upserts).toEqual([])
    expect(feed.to_revision).toBe(first.snapshot_revision)
  })
})
