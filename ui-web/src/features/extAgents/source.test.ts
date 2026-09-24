// @vitest-environment happy-dom
/* The whitelist that turns one `subagents.list` row into what the page draws,
 * and the memory that keeps a probe-less refetch from blanking a health line
 * the reader just saw.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { FixtureTransport } from '../../rpc/fixtureTransport'
import { setGateway } from '../../rpc/gateway'
import { resetExtAgentsSeen, extAgentsFetch, extAgentRowOf, extAgentsSource, isFound, sectionOf, stageOf, wizardSection } from './source'

import type { ExtAgentRowWire, Section } from './source'
import type { ExtAgentRow } from './types'

const wire = (over: Partial<ExtAgentRowWire>): ExtAgentRowWire => ({ name: 'codex', ...over } as ExtAgentRowWire)

/* A row with every field `ExtAgentRow` requires, for the classifier table
   below -- unlike `card()`, which only fills what one write-test call reads. */
function fullRow(over: Partial<ExtAgentRow> = {}): ExtAgentRow {
  return {
    name: 'agent',
    kind: 'cli',
    configured: false,
    enabled: false,
    probe_status: 'unknown',
    probe_detail: '',
    has_api_key: false,
    description: '',
    test_running: false,
    last_test_ok: null,
    last_test_at_ms: null,
    last_test_detail: '',
    ...over,
  }
}

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
  /* A remedy is data the sheet renders, so only a well-formed one is kept: a
     kind the sheet has no words for would draw an empty fix where the server's
     sentence used to be. */
  it('reads the fix a failed test named, and drops anything that is not one', () => {
    const fixed = extAgentRowOf(wire({ last_test_remedy: { kind: 'setup', command: 'hermes model' } }))
    expect(fixed.last_test_remedy).toEqual({ kind: 'setup', command: 'hermes model' })
    const keyOnly = extAgentRowOf(wire({ last_test_remedy: { kind: 'api_key' } }))
    expect(keyOnly.last_test_remedy).toEqual({ kind: 'api_key', command: '' })
    expect('last_test_remedy' in extAgentRowOf(wire({}))).toBe(false)
    const junk = wire({ last_test_remedy: { kind: 'reboot' } as unknown as ExtAgentRowWire['last_test_remedy'] })
    expect('last_test_remedy' in extAgentRowOf(junk)).toBe(false)
    const fetch = extAgentRowOf(wire({ last_test_remedy: { kind: 'download', command: 'npx -y a@1' } }))
    expect(fetch.last_test_remedy).toEqual({ kind: 'download', command: 'npx -y a@1' })
  })

  /* What to install, as the executable the probe did not find. Absent unless
     sent, so a server that predates it reads as "nothing named". */
  it('carries the executable a missing row lacks', () => {
    expect(extAgentRowOf(wire({ probe_status: 'missing', probe_missing: 'npx' })).probe_missing).toBe('npx')
    expect('probe_missing' in extAgentRowOf(wire({}))).toBe(false)
    /* The server sends null on every row that is not missing. */
    const unnamed = wire({ probe_missing: null as unknown as string })
    expect('probe_missing' in extAgentRowOf(unnamed)).toBe(false)
  })

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

  /* With what it found absent: dropped, a missing npx row's sheet went back to
     offering the agent's own installer after any write. */
  it('carries what a missing row lacks along with its verdict', async () => {
    listing([wire({ probe_status: 'missing', probe_missing: 'npx' })], [wire({})])
    await extAgentsFetch(true)
    const [row] = await extAgentsFetch(false)
    expect(row).toMatchObject({ probe_status: 'missing', probe_missing: 'npx' })
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

/* The onboarding wizard's two buckets, over the same row the settings page's
   `stageOf`/`sectionOf` reads -- moved here from ExtAgentsPage.tsx so
   both live beside the row shape they classify. */
describe('the wizard step sections', () => {
  const cases: Array<[string, Partial<ExtAgentRow>, Section, 'on' | 'avail' | null, boolean]> = [
    // label, row, sectionOf (the hub's), wizardSection, isFound
    ['the built-in agent', { builtin: true, enabled: true, probe_status: 'unknown' }, 'on', null, false],
    ['a vendored agent switched on', { vendored: true, enabled: true, probe_status: 'ready' }, 'on', 'on', false],
    /* Drawn as available -- the connected section switched it off and it needs
       somewhere to come back from -- but never counted: the step is about
       connecting something external. */
    ['a vendored agent switched off', { vendored: true, enabled: false, probe_status: 'attention' }, 'avail', 'avail', false],
    ['a preset this machine has, unverified', { configured: false, enabled: false, probe_status: 'attention' }, 'avail', 'avail', true],
    ['a preset never probed', { configured: false, enabled: false, probe_status: 'unknown' }, 'avail', 'avail', true],
    /* The hub's third section, not a first run's decision: an install is not a
       wizard step. */
    ['a preset this machine has never had', { kind: 'cli', configured: false, enabled: false, probe_status: 'missing' }, 'missing', null, false],
    /* An older server's build-in-flight flag: the hub files it under
       available; the step offers nothing, since its Connect would toggle an
       install that is not finished. */
    ['a preset mid-build on an older server', { configured: false, enabled: false, building: true, probe_status: 'ready' }, 'avail', null, false],
    ['a configured agent switched on', { configured: true, enabled: true, probe_status: 'ready' }, 'on', 'on', true],
    ['a configured agent switched off', { configured: true, enabled: false, probe_status: 'ready' }, 'avail', 'avail', true],
    [
      'a configured agent off with its preset moved to another transport',
      { configured: true, enabled: false, probe_status: 'ready', upgrade_to: 'acp' },
      'avail',
      'avail',
      true,
    ],
    /* An endpoint, not a command on the machine: its connect is a key typed
       into the hub's sheet, which the step has not got. */
    ['an openai row with no key', { kind: 'openai', configured: false, enabled: false, has_api_key: false, probe_status: 'ready' }, 'avail', null, false],
    ['an openai row with a key, switched on', { kind: 'openai', configured: true, enabled: true, has_api_key: true, probe_status: 'ready' }, 'on', null, false],
  ]

  it.each(cases)('%s', (_label, over, hub, section, found) => {
    const row = fullRow(over)
    expect(sectionOf(row)).toBe(hub)
    expect(wizardSection(row)).toBe(section)
    expect(isFound(row)).toBe(found)
  })
})

/* A refusal the handshake already measured is named on the row instead of
   offered as a Connect that would spend a launch to be refused again. */
describe('a credential refusal the handshake measured', () => {
  it('rides the wire as needs_auth, and reads false from a server that predates it', () => {
    expect(extAgentRowOf(wire({ needs_auth: true })).needs_auth).toBe(true)
    expect(extAgentRowOf(wire({})).needs_auth).toBe(false)
  })

  it('names the row unauthorized only while it is off the roster', () => {
    const refused: Partial<ExtAgentRow> = {
      kind: 'acp',
      configured: false,
      enabled: false,
      probe_status: 'attention',
      needs_auth: true,
    }
    expect(stageOf(fullRow(refused))).toBe('unauthorized')
    expect(sectionOf(fullRow(refused))).toBe('avail')
    /* Connected: Disconnect stays whatever the credential has since done. */
    expect(stageOf(fullRow({ ...refused, configured: true, enabled: true }))).toBe('live')
    /* An endpoint's credential is the key field, settled by its own probe. */
    expect(stageOf(fullRow({ ...refused, kind: 'openai' }))).toBe('key')
    /* The same row without the refusal is a preset to add. */
    expect(stageOf(fullRow({ ...refused, needs_auth: false }))).toBe('add')
  })
})
