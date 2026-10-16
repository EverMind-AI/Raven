/* The registered machines and what the Agent Hub's machines tab asks of them.
 *
 * Offline stand-ins for `machines.*`: two ssh rows -- one with a node and a
 * link up, one registered but never reached -- a register that writes a third
 * (or refuses, by the id), a node install that is "running" for a few reads
 * and then done, the way the server's background task reports through
 * `machines.list`, and a removal that refuses the one row agents run on.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

type Row = ResultOf<'machines.list'>['rows'][number]

const ROWS: Row[] = [
  {
    id: 'ravenx2', name: 'RavenX 2', transport: 'ssh', state: 'idle', detail: '', agents: 0,
    node_version: '0.2.4', node_current: true,
    install: { running: false, ok: true, detail: 'Installed the Raven node 0.2.4-f5bd06f5d991 in /root/.raven-node (62 MB). The node answers: Python 3.12.3.' },
    paths: ['/root/raven-work'], problems: [],
  },
  {
    id: 'gpu1', name: 'GPU box 1', transport: 'ssh', state: 'off', detail: '', agents: 0,
    node_current: false, install: { running: false, detail: '' },
    paths: ['/data/alice/projects'], problems: ["gpu1: 'key' is missing, so there is no way onto it"],
  },
]

/* How a register refuses, by the id typed: one spelling per refusal the page
   explains, so the offline page shows each of them. */
const REFUSALS: Record<string, [ResultOf<'machines.add'>['reason'], string]> = {
  down: ['unreachable', 'could not reach the machine: Connection refused'],
  locked: ['login', 'could not reach the machine: Permission denied (publickey).'],
  changed: ['hostkey', 'could not reach the machine: Host key verification failed.'],
  slow: ['timeout', 'could not reach the machine: no answer within 30s'],
}

export function createMachines(_env: FixtureEnv): { fixtures: Fixtures } {
  let rows = ROWS.map((r) => ({ ...r }))
  let installReads = 0
  const copy = (r: Row): Row => ({ ...r, install: { ...r.install }, paths: [...r.paths], problems: [...r.problems] })
  const fixtures: Fixtures = {
    'machines.list': async (p) => {
      const running = rows.find((r) => r.install.running)
      if (running && ++installReads > 2) {
        running.install = { running: false, ok: true, detail: `Installed the Raven node 0.2.4-f5bd06f5d991 in ${running.install.detail} (62 MB). The node answers: Python 3.12.3.` }
        running.node_version = '0.2.4'
        running.node_current = true
        running.state = 'idle'
      }
      if (p.refresh) {
        for (const r of rows) {
          if (p.id && r.id !== p.id) continue
          if (r.transport !== 'ssh') continue
          if (r.node_current) r.state = 'idle'
          else {
            r.state = 'error'
            r.detail = `no Raven node on machine '${r.name}' (${r.id}); install it with \`raven ops connection install-node ${r.id}\``
          }
        }
      }
      return { rows: rows.map(copy) }
    },
    'machines.add': async (p) => {
      const id = String(p.id || '').trim()
      if (!id) return { ok: false, detail: "'id' is missing", reason: 'invalid' }
      if (rows.some((r) => r.id === id)) return { ok: false, detail: `a machine with id '${id}' is registered already`, reason: 'duplicate' }
      const refusal = REFUSALS[id]
      if (refusal) return { ok: false, detail: refusal[1], reason: refusal[0] }
      const row: Row = {
        id, name: String(p.name || id), transport: 'ssh', state: 'off', detail: '', agents: 0,
        node_current: false, install: { running: false, detail: '' },
        paths: Array.isArray(p.paths) ? p.paths.map(String) : [], problems: [],
      }
      rows = [...rows, row]
      return { ok: true, detail: `reached machine '${row.name}' (${id}): 32 cores, 128 GB, 2 x NVIDIA A800`, reason: '', row: copy(row) }
    },
    'machines.install_node': async (p) => {
      const row = rows.find((r) => r.id === p.id)
      if (!row) return { started: false, detail: `machine '${String(p.id)}' is not registered; add it with \`raven ops connection add\`` }
      if (row.install.running) return { started: false, detail: `an install is already running on machine '${row.id}'` }
      installReads = 0
      /* The directory rides in the detail so the end can name it. */
      row.install = { running: true, detail: String(p.node_dir || '~/.raven-node') }
      return { started: true, detail: `installing the Raven node on machine '${row.name}' (${row.id})` }
    },
    'machines.doctor': async () => ({
      problems: rows.flatMap((r) => r.problems.map((detail) => ({ id: r.id, blocking: true, detail }))),
    }),
    'machines.remove': async (p) => {
      const row = rows.find((r) => r.id === p.id)
      if (!row) return { removed: false, detail: `machine '${String(p.id)}' is not registered`, agents: [] }
      if (row.id === 'ravenx2') {
        return {
          removed: false,
          detail: `1 agent entry names machine '${row.name}' (${row.id}) (claude_code@ravenx2); disconnect them first`,
          agents: ['claude_code@ravenx2'],
        }
      }
      rows = rows.filter((r) => r !== row)
      return { removed: true, detail: `removed machine '${row.name}' (${row.id}) from the registry; its ssh alias is gone from ~/.ssh/config; the node installed on it stays there`, agents: [] }
    },
  }
  return { fixtures }
}
