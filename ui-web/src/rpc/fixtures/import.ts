/* The cold-start import, canned: what a scan finds on a machine that has run
 * Claude Code and Hermes, and an import that finishes a few seconds after it
 * starts -- the message pass, then the two phases behind it.
 *
 * Every platform the importer knows is listed, the way the gateway lists them;
 * the two with scanners carry counts, the rest say so. The run's progress is
 * scheduled on the injected clock so two libraries answer the same bytes. A
 * full import lets one Hermes conversation fail, so the finished row has a
 * failure to show; a stop mid-run leaves the counts short, which is the row
 * the rail draws as paused.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

type Platform = ResultOf<'import.scan'>['platforms'][number]
type Status = ResultOf<'import.status'>
type Counts = Status['by_platform'][string]
type Phase = NonNullable<Status['phase']>
type Tier = 'memory_files' | 'full'

const PLATFORMS: Platform[] = [
  { platform: 'claude_code', scannable: true, memory_files: 31, conversations: 284, estimated_size: 18_400_000 },
  { platform: 'codex', scannable: false, memory_files: 0, conversations: 0, estimated_size: 0 },
  { platform: 'kimicode', scannable: false, memory_files: 0, conversations: 0, estimated_size: 0 },
  { platform: 'hermes', scannable: true, memory_files: 8, conversations: 52, estimated_size: 2_100_000 },
  { platform: 'openclaw', scannable: false, memory_files: 0, conversations: 0, estimated_size: 0 },
]

/* The message pass, in five steps; then the profile phase and the skill
   phase, each a step of their own. */
const RUN_MS = 2500
const STEPS = 5
const PHASE_MS = 600

export interface ImportFixture {
  fixtures: Fixtures
}

export function createImport(env: FixtureEnv): ImportFixture {
  let running = false
  let run = 0
  let tier: Tier | undefined
  let platforms: string[] = []
  let phase: Phase | undefined
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

  /* Every scheduled step checks it still belongs to the run that scheduled it:
     a stop, or a later start, must not be advanced by the earlier run's clock. */
  const when = (ms: number, fn: () => void): void => {
    const mine = run
    env.schedule(ms, () => { if (running && run === mine) fn() })
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
          /* A run that stopped short keeps what it landed; the same request again
             skips it, the way the importer's state file makes the gateway do. */
          const had = by[row.platform]
          by[row.platform] = { total: n, submitted: had && had.total === n ? had.submitted : 0, failed: 0 }
          total += n
        }
        if (!total) return { started: false, total: 0, detail: 'nothing to import' }
        running = true
        run += 1
        tier = p.tier
        platforms = picked.map((row) => row.platform)
        phase = undefined
        for (let step = 1; step <= STEPS; step += 1) {
          when((RUN_MS * step) / STEPS, () => {
            for (const c of Object.values(by)) c.submitted = Math.max(c.submitted, Math.round((c.total * step) / STEPS))
            if (step === STEPS && p.tier === 'full' && by.hermes) {
              by.hermes.submitted -= 1
              by.hermes.failed = 1
            }
          })
        }
        when(RUN_MS + PHASE_MS * 0.5, () => { phase = { kind: 'profile', current: 1, total: 3 } })
        when(RUN_MS + PHASE_MS * 1.0, () => { phase = { kind: 'profile', current: 3, total: 3 } })
        when(RUN_MS + PHASE_MS * 1.5, () => { phase = { kind: 'skills', current: 0, total: 2 } })
        when(RUN_MS + PHASE_MS * 2.0, () => { phase = { kind: 'skills', current: 2, total: 2 } })
        when(RUN_MS + PHASE_MS * 2.5, () => { phase = undefined; running = false })
        return { started: true, total, detail: '' }
      },
      'import.status': () => ({
        running,
        ...totals(),
        by_platform: Object.fromEntries(Object.entries(by).map(([k, v]) => [k, { ...v }])),
        phase: phase ? { ...phase } : null,
        tier: tier ?? null,
        platforms: [...platforms],
      }),
      'import.stop': () => {
        const was = running
        running = false
        phase = undefined
        return { stopped: was }
      },
    },
  }
}
