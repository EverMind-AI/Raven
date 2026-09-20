/* The cold-start import, canned: what a scan finds on a machine that has run
 * Claude Code and Hermes, and an import that finishes a few seconds after it
 * starts.
 *
 * Every platform the importer knows is listed, the way the gateway lists them;
 * the two with scanners carry counts, the rest say so. The run's progress is
 * scheduled on the injected clock so two libraries answer the same bytes.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

type Platform = ResultOf<'import.scan'>['platforms'][number]
type Counts = ResultOf<'import.status'>['by_platform'][string]

const PLATFORMS: Platform[] = [
  { platform: 'claude_code', scannable: true, memory_files: 31, conversations: 284, estimated_size: 18_400_000 },
  { platform: 'codex', scannable: false, memory_files: 0, conversations: 0, estimated_size: 0 },
  { platform: 'kimicode', scannable: false, memory_files: 0, conversations: 0, estimated_size: 0 },
  { platform: 'hermes', scannable: true, memory_files: 8, conversations: 52, estimated_size: 2_100_000 },
  { platform: 'openclaw', scannable: false, memory_files: 0, conversations: 0, estimated_size: 0 },
]

const RUN_MS = 2500

export interface ImportFixture {
  fixtures: Fixtures
}

export function createImport(env: FixtureEnv): ImportFixture {
  let running = false
  const by: Record<string, Counts> = {}

  const totals = (): { total: number; submitted: number; failed: number } => {
    let total = 0
    let submitted = 0
    let failed = 0
    for (const c of Object.values(by)) {
      total += c.total
      submitted += c.submitted
      failed += c.failed
    }
    return { total, submitted, failed }
  }

  return {
    fixtures: {
      'import.scan': () => ({ ready: true, reason: '', platforms: PLATFORMS.map((p) => ({ ...p })) }),
      'import.run': (p) => {
        if (running) return { started: false, total: 0, detail: 'an import is already running' }
        const picked = PLATFORMS.filter((row) => row.scannable && (p.platforms ?? []).includes(row.platform))
        let total = 0
        for (const row of picked) {
          const n = row.memory_files + (p.tier === 'full' ? row.conversations : 0)
          by[row.platform] = { total: n, submitted: 0, failed: 0 }
          total += n
        }
        if (!total) return { started: false, total: 0, detail: 'nothing to import' }
        running = true
        env.schedule(RUN_MS, () => {
          running = false
          for (const c of Object.values(by)) c.submitted = c.total
        })
        return { started: true, total, detail: '' }
      },
      'import.status': () => ({ running, ...totals(), by_platform: Object.fromEntries(Object.entries(by).map(([k, v]) => [k, { ...v }])) }),
      'import.stop': () => {
        const was = running
        running = false
        return { stopped: was }
      },
    },
  }
}
