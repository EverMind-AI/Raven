/* The saved Personas, as the offline page reads them.
 *
 * A Persona IS a stored artifact, so the wall reads the playbook library and
 * narrows it to the rows that carry a coordinator seat
 * (features/persona/store.ts's `isPersona`). The rows here are wire-shaped for
 * that reason: the island is the same code in both modes, and a fixture in the
 * island's own shape would hide a mapping bug until live.
 *
 * The library is a variable rather than a constant because the page can change
 * it: a delete takes a row out here the way it takes a directory out there, so
 * the offline wall answers a click instead of redrawing what was just removed.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { PlaybookRow } from '../generated'

export interface PersonaFixture {
  fixtures: Fixtures
}

const persona = (over: Partial<PlaybookRow> & { name: string }): PlaybookRow => ({
  description: '',
  task_summary: '',
  schema_version: 2,
  artifact_kind: 'harness',
  coordinator: true,
  coordinator_brief: '',
  mode: 'prompt',
  confirm: false,
  nodes: [],
  workers: [],
  origin: 'user',
  disabled: false,
  error: '',
  ...over,
})

export function createPersona(_env: FixtureEnv): PersonaFixture {
  let kept: PlaybookRow[] = [
    persona({
      name: 'travel-concierge',
      description:
        'Turns a destination, dates, a budget, companions and a pace into a six-part brief you can walk',
      coordinator_brief: 'Collects the trip’s five required facts, then hands the digging and the layout on',
      workers: [
        { label: 'destination-intel', agent: 'Raven-Research' },
        { label: 'brief-editor', agent: 'Raven-Design' },
      ],
    }),
    persona({
      name: 'skeptical-fact-checker',
      description: 'Finds a primary source for every claim, and says so plainly when there is none',
      coordinator_brief: 'Refuses a claim it cannot source, and says which part it could not',
      workers: [{ label: 'evidence', agent: 'Raven-Research' }],
    }),
  ]

  /* What a describing turn would have left behind. The offline page has no
     engine, so the draft is here from the start rather than after a turn. */
  let held: PlaybookRow | null = persona({
    name: 'nightly-release-captain',
    description: 'Watches the release channel, chases what broke, and writes the morning note',
    coordinator_brief: 'Watches the release channel and hands the digging to oncall',
    workers: [{ label: 'triage', agent: 'Raven-Oncall' }],
    origin: 'draft',
  })

  return {
    fixtures: {
      'playbooks.list': () => ({ playbooks: kept.slice() }),
      'playbooks.delete': (p) => {
        const name = (p as { name?: string }).name || ''
        const before = kept.length
        kept = kept.filter((row) => row.name !== name)
        return { name, deleted: kept.length < before, uncovered_builtin: false }
      },
      /* The offline page can be walked end to end: describing one leaves this
         draft, saving it answers a name, and discarding clears it. Held in a
         closure rather than written anywhere, which is what the engine does
         with a real one until someone keeps it. */
      'playbooks.draft': () => ({ draft: held }),
      'playbooks.draft_save': (p) => {
        const name = (p as { name?: string }).name || 'nightly-release-captain'
        if (held) kept = [...kept, persona({ ...held, name, origin: 'user' })]
        held = null
        return { name }
      },
      'playbooks.draft_discard': () => {
        const had = held !== null
        held = null
        return { discarded: had }
      },
    },
  }
}
