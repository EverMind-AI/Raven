import { afterEach, describe, expect, it } from 'vitest'

import { absorb, resetCapabilities, servesTrajectory } from '../capabilities'
import { FixtureTransport } from '../fixtureTransport'
import { demoFixtures } from './index'

afterEach(() => {
  resetCapabilities()
})

const transport = () => new FixtureTransport(demoFixtures, { now: () => 1789000000000, timer: (_ms, fn) => fn() })

describe('the offline trajectory library', () => {
  it('is announced by the offline handshake, so the entry can appear', async () => {
    const hello = await transport().call('system.hello', { client_version: '0.1.0' })
    expect(servesTrajectory()).toBe(false)
    absorb(hello.server_capabilities)
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
})
