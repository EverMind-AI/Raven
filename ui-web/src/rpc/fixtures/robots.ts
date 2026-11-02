/* Offline personal robot records; checks never contact or claim a physical device. */
import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

type Row = ResultOf<'robots.list'>['devices'][number]

export function createRobots(env: FixtureEnv): { fixtures: Fixtures } {
  const rows = new Map<string, Row>()
  let sequence = 0
  const stamp = (): string => new Date(env.now()).toISOString()
  const get = (id: string): Row => {
    const row = rows.get(id)
    if (!row) throw new Error('Registration not found')
    return row
  }
  const copy = (row: Row): Row => ({ ...row, observation: row.observation ? { ...row.observation, observed: { ...row.observation.observed } } : null })
  const fixtures: Fixtures = {
    'robots.list': (params) => ({ devices: [...rows.values()].filter((row) => !!row.deleted_at === !!params.trash).map(copy), status_ttl_seconds: 60 }),
    'robots.save': (params) => {
      if (!params.device.confirm_personal) throw new Error('Personal device confirmation required')
      const previous = params.id ? get(params.id) : null
      if (previous && (previous.deleted_at || previous.revision !== params.revision)) throw new Error('Registration changed')
      const { confirm_personal: _confirmation, ...data } = params.device
      const id = params.id ?? (++sequence).toString(16).padStart(32, '0')
      const row: Row = {
        model: '', location: '', note: '', expected_project: '', status_url: '', connection_mode: 'metadata',
        ...data, id, revision: (previous?.revision ?? 0) + 1,
        status: data.connection_mode === 'http_status' ? 'not_connected' : 'unknown',
        observation: null, stale: false, deleted_at: null, updated_at: stamp(),
      }
      rows.set(id, row)
      return { device: copy(row) }
    },
    'robots.remove': ({ id }) => {
      const row = get(id); row.deleted_at = stamp(); row.revision += 1
      return { ok: true }
    },
    'robots.restore': ({ id }) => {
      const row = get(id); row.deleted_at = null; row.observation = null; row.revision += 1; row.updated_at = stamp()
      row.status = row.connection_mode === 'metadata' ? 'unknown' : 'not_connected'
      return { device: copy(row) }
    },
    'robots.check': ({ id }) => {
      const row = get(id)
      if (row.deleted_at || row.connection_mode !== 'http_status') throw new Error('No status endpoint')
      row.status = 'not_connected'
      row.observation = { status: 'not_connected', checked_at: stamp(), checked_epoch: env.now() / 1000, http_status: null, detail: 'Offline fixtures never contact physical devices.', observed: {} }
      return { device: copy(row) }
    },
  }
  return { fixtures }
}
