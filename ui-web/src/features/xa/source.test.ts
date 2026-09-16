// @vitest-environment happy-dom
/* The whitelist that turns one `subagents.list` row into what the page draws,
 * and the memory that keeps a probe-less refetch from blanking a health line
 * the reader just saw. Both were untested while they lived in the legacy layer.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { resetXaSeen, xaFetch, xaRowOf } from './source'
import { FixtureTransport } from '../../rpc/fixtureTransport'
import { setGateway } from '../../state/gateway'

import type { XaRowWire } from './source'

const wire = (over: Partial<XaRowWire>): XaRowWire => ({ name: 'codex', ...over } as XaRowWire)

/* A transport whose `subagents.list` answers from a queue, so a test can play
   a probing read and the probe-less read that follows it. */
function listing(...answers: XaRowWire[][]): { probes: boolean[] } {
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
  resetXaSeen()
  vi.restoreAllMocks()
})

describe('one agent row', () => {
  it('names its kind from the flag when the server sends none', () => {
    expect(xaRowOf(wire({ builtin: true })).kind).toBe('builtin')
    expect(xaRowOf(wire({})).kind).toBe('cli')
    expect(xaRowOf(wire({ kind: 'acp' })).kind).toBe('acp')
  })

  /* Three facts kept apart: knowing about it, being allowed to dispatch to it,
     and being able to run it. */
  it('keeps configured, enabled and the probe verdict apart', () => {
    const row = xaRowOf(wire({ configured: false, enabled: true, probe_status: 'ready' }))
    expect(row).toMatchObject({ configured: false, enabled: true, probe_status: 'ready' })
  })

  it('calls a verdict it was not given unknown', () => {
    expect(xaRowOf(wire({})).probe_status).toBe('unknown')
  })

  /* A field this mapper does not name is a field the island never sees. */
  it('is a whitelist, so an unnamed field does not reach the page', () => {
    const row = xaRowOf(wire({ command: ['codex', '--json'] } as Partial<XaRowWire>))
    expect(row).not.toHaveProperty('command')
  })

  it('reads a missing test verdict as no verdict rather than as a failure', () => {
    expect(xaRowOf(wire({})).last_test_ok).toBe(null)
    expect(xaRowOf(wire({ last_test_ok: false })).last_test_ok).toBe(false)
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
    await xaFetch(true)
    const [row] = await xaFetch(false)
    expect(probes).toEqual([true, false])
    expect(row).toMatchObject({ probe_status: 'ready', probe_detail: 'v2 on PATH' })
  })

  it('lets a probing answer overwrite what it remembered', async () => {
    listing(
      [wire({ probe_status: 'ready', probe_detail: 'v2 on PATH' })],
      [wire({ probe_status: 'missing', probe_detail: 'not on PATH' })],
    )
    await xaFetch(true)
    const [row] = await xaFetch(true)
    expect(row).toMatchObject({ probe_status: 'missing', probe_detail: 'not on PATH' })
  })

  it('remembers nothing about an agent it has not seen', async () => {
    listing([wire({ name: 'claude', probe_status: 'ready' })], [wire({ name: 'codex' })])
    await xaFetch(true)
    const [row] = await xaFetch(false)
    expect(row).toMatchObject({ name: 'codex', probe_status: 'unknown' })
  })

  it('forgets an agent that has left the list', async () => {
    listing(
      [wire({ probe_status: 'ready' })],
      [],
      [wire({})],
    )
    await xaFetch(true)
    await xaFetch(false)
    const [row] = await xaFetch(false)
    expect(row?.probe_status).toBe('unknown')
  })
})
