// @vitest-environment happy-dom
/* The whitelist that turns one `subagents.list` row into what the page draws,
 * and the memory that keeps a probe-less refetch from blanking a health line
 * the reader just saw. Both were untested while they lived in the legacy layer.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { FixtureTransport } from '../../rpc/fixtureTransport'
import { setGateway } from '../../rpc/gateway'
import { resetExtAgentsSeen, extAgentsFetch, extAgentRowOf, extAgentsSource } from './source'

import type { ExtAgentRowWire } from './source'
import type { ExtAgentRow } from './types'

const wire = (over: Partial<ExtAgentRowWire>): ExtAgentRowWire => ({ name: 'codex', ...over } as ExtAgentRowWire)

/* A transport whose `subagents.list` answers from a queue, so a test can play
   a probing read and the probe-less read that follows it. */
function listing(...answers: ExtAgentRowWire[][]): { probes: boolean[] } {
  const probes: boolean[] = []
  const transport = new FixtureTransport({})
  transport.call = (async (method: string, params: { probe?: boolean }) => {
    expect(method).toBe('subagents.list')
    probes.push(!!params.probe)
    return { rows: answers.shift() ?? [] }
  }) as typeof transport.call
  setGateway(transport)
  return { probes }
}

beforeEach(() => {
  resetExtAgentsSeen()
  vi.restoreAllMocks()
})

describe('one agent row', () => {
  it('names its kind from the flag when the server sends none', () => {
    expect(extAgentRowOf(wire({ builtin: true })).kind).toBe('builtin')
    expect(extAgentRowOf(wire({})).kind).toBe('cli')
    expect(extAgentRowOf(wire({ kind: 'acp' })).kind).toBe('acp')
  })

  /* Three facts kept apart: knowing about it, being allowed to dispatch to it,
     and being able to run it. */
  it('keeps configured, enabled and the probe verdict apart', () => {
    const row = extAgentRowOf(wire({ configured: false, enabled: true, probe_status: 'ready' }))
    expect(row).toMatchObject({ configured: false, enabled: true, probe_status: 'ready' })
  })

  it('calls a verdict it was not given unknown', () => {
    expect(extAgentRowOf(wire({})).probe_status).toBe('unknown')
  })

  /* A field this mapper does not name is a field the island never sees. */
  it('is a whitelist, so an unnamed field does not reach the page', () => {
    const row = extAgentRowOf(wire({ command: ['codex', '--json'] } as Partial<ExtAgentRowWire>))
    expect(row).not.toHaveProperty('command')
  })

  it('reads a missing test verdict as no verdict rather than as a failure', () => {
    expect(extAgentRowOf(wire({})).last_test_ok).toBe(null)
    expect(extAgentRowOf(wire({ last_test_ok: false })).last_test_ok).toBe(false)
  })
})

describe('refetching the list', () => {
  /* A probe-less list reports every row as "unknown", which would blank the
     health line of a row that was ready a second ago -- connecting an agent
     would look like it broke it. */
  it('carries the last known verdict over a probe-less answer', async () => {
    const { probes } = listing(
      [wire({ probe_status: 'ready', probe_detail: 'v2 on PATH' })],
      [wire({})],
    )
    await extAgentsFetch(true)
    const [row] = await extAgentsFetch(false)
    expect(probes).toEqual([true, false])
    expect(row).toMatchObject({ probe_status: 'ready', probe_detail: 'v2 on PATH' })
  })

  it('lets a probing answer overwrite what it remembered', async () => {
    listing(
      [wire({ probe_status: 'ready', probe_detail: 'v2 on PATH' })],
      [wire({ probe_status: 'missing', probe_detail: 'not on PATH' })],
    )
    await extAgentsFetch(true)
    const [row] = await extAgentsFetch(true)
    expect(row).toMatchObject({ probe_status: 'missing', probe_detail: 'not on PATH' })
  })

  it('remembers nothing about an agent it has not seen', async () => {
    listing([wire({ name: 'claude', probe_status: 'ready' })], [wire({ name: 'codex' })])
    await extAgentsFetch(true)
    const [row] = await extAgentsFetch(false)
    expect(row).toMatchObject({ name: 'codex', probe_status: 'unknown' })
  })

  it('forgets an agent that has left the list', async () => {
    listing(
      [wire({ probe_status: 'ready' })],
      [],
      [wire({})],
    )
    await extAgentsFetch(true)
    await extAgentsFetch(false)
    const [row] = await extAgentsFetch(false)
    expect(row?.probe_status).toBe('unknown')
  })
})

/* Every write the cards can make, and the re-read each one ends with: the list
   is refetched after a write rather than patched locally, so a row the server
   shaped differently than the page expected cannot drift. */
function writes(): { asked: Array<[string, Record<string, unknown>]> } {
  const asked: Array<[string, Record<string, unknown>]> = []
  const transport = new FixtureTransport({})
  transport.call = (async (method: string, params: Record<string, unknown>) => {
    asked.push([method, params])
    return { rows: [] }
  }) as typeof transport.call
  setGateway(transport)
  return { asked }
}

const card = (over: Partial<ExtAgentRow> = {}): ExtAgentRow =>
  ({ name: 'codex', preset: 'codex-cli', description: 'the cli one', configured: true, ...over } as ExtAgentRow)

describe('the seven writes a card can make', () => {
  it('connects with the preset and the three fields a person can type', async () => {
    /* Only name / description / key travel: every execution field comes from
       the preset server-side, because a page that could post a command line
       would make "which agent is this" unanswerable. */
    const { asked } = writes()

    await extAgentsSource.act('connect', card({ preset: 'codex-cli' }), { new_name: 'mine', description: 'd', api_key: 'k' })

    expect(asked).toEqual([
      ['subagents.add', { preset: 'codex-cli', name: 'mine', description: 'd', api_key: 'k' }],
      ['subagents.list', { probe: false }],
    ])
  })

  it('updates without renaming when the name did not change', async () => {
    const { asked } = writes()

    await extAgentsSource.act('update', card(), { new_name: 'codex', description: 'a new line' })

    expect(asked[0]).toEqual(['subagents.update', {
      name: 'codex', new_name: undefined, description: 'a new line', api_key: undefined,
    }])
  })

  it('toggles one switch for every kind of row', async () => {
    const { asked } = writes()

    await extAgentsSource.act('toggle', card(), { enabled: true })

    expect(asked[0]).toEqual(['subagents.toggle', { name: 'codex', enabled: true }])
  })

  it('migrates as a remove and an add, in that order', async () => {
    /* A stale transport is the one connect that cannot be a flag: there is no
       "change the transport" write, so it is an entry replaced rather than an
       entry mutated underneath its session handles. */
    const { asked } = writes()

    await extAgentsSource.act('migrate', card())

    expect(asked.map(([method]) => method))
      .toEqual(['subagents.remove', 'subagents.add', 'subagents.list'])
    expect(asked[1]).toEqual(['subagents.add', {
      preset: 'codex-cli', name: 'codex', description: 'the cli one',
    }])
  })

  it('tests a configured row against its config, and an unconfigured one against its preset', async () => {
    /* The one call here that spends the agent's own quota, which is why nothing
       runs it on page load. */
    const { asked } = writes()

    await extAgentsSource.act('test', card({ configured: true }))
    await extAgentsSource.act('test', card({ configured: false }))

    expect(asked.filter(([m]) => m === 'subagents.test').map(([, p]) => p.source))
      .toEqual(['config', 'preset'])
  })

  it('cancels a test by name alone', async () => {
    const { asked } = writes()

    await extAgentsSource.act('test_cancel', card())

    expect(asked[0]).toEqual(['subagents.test_cancel', { name: 'codex' }])
  })

  it('starts a build by name alone', async () => {
    const { asked } = writes()

    await extAgentsSource.act('build', card())

    expect(asked[0]).toEqual(['subagents.build', { name: 'codex' }])
  })
})
