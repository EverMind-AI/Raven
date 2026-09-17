/* The scheduled work this install has.
 *
 * The four canned jobs the offline canvas has always listed, now as `cron.list`
 * sends them -- which is where every date on the page went: the rows used to
 * carry their own worded stamps ("every day 08:00", "tomorrow 08:00",
 * "yesterday 19:00") and now carry instants, which `cronToRow` and `fmtStamp`
 * word against the reader's own clock and language.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'

type Job = ResultOf<'cron.list'>['jobs'][number]
type Run = ResultOf<'cron.runs'>['runs'][number]

const HOUR = 3600000
const DAY = 86400000

/* One job, plus the runs it has recorded. `at` is the time of day the job
   fires, which is all the expressions below say; the instants the rows draw
   are derived from it against `now()`. */
interface Fixture {
  id: string
  name: string
  enabled: boolean
  message: string
  kind: Job['kind']
  expr: string
  /** Last fire, in ms before now. */
  last?: number
  /** Next fire, in ms after now. */
  next?: number
  status?: Job['last_status']
  runs: Array<{ ago: number; ok: boolean; note: string }>
}

const JOBS: Fixture[] = [
  { id: 'j1', name: '昨日错误日志汇总', enabled: true,
    message: '读 ~/logs 下昨天的日志，按错误类型分组，超过 10 次的单独列出，写成一段简报。',
    kind: 'cron', expr: '0 8 * * *', last: 3 * HOUR, next: 21 * HOUR, status: 'ok',
    runs: [{ ago: 3 * HOUR, ok: true, note: '3 类错误 · 支付回调占 68%' },
      { ago: DAY + 3 * HOUR, ok: true, note: '2 类错误' }] },
  { id: 'j2', name: '依赖安全告警', enabled: false,
    message: '跑一遍依赖扫描，只报 high 及以上，附上可直接执行的升级命令。',
    kind: 'cron', expr: '30 9 * * 1', last: 3 * DAY, status: 'ok',
    runs: [{ ago: 3 * DAY, ok: true, note: '2 个 high · 已给升级命令' }] },
  { id: 'j3', name: '竞品动态', enabled: true,
    message: '抓 Clay / 11x / Unify 的官网和博客，只报和上次相比的变化。',
    kind: 'cron', expr: '0 19 * * *', last: 8 * HOUR, next: 16 * HOUR, status: 'error',
    runs: [{ ago: 8 * HOUR, ok: false, note: '网页搜索未配置，只抓到 1 家' }] },
  { id: 'j4', name: '周报草稿', enabled: true,
    message: '汇总本周的 commit 和已关闭的 issue，写成周报初稿。',
    kind: 'cron', expr: '0 17 * * 5', next: 2 * DAY, status: 'ok', runs: [] },
]

export interface CronFixture {
  fixtures: Fixtures
}

export function createCron(env: FixtureEnv): CronFixture {
  /* Held as state for the same reason every other table here is: a toggle, a
     save and a delete all change what the next `cron.list` answers. */
  const jobs = JOBS.map((j) => ({ ...j, runs: j.runs.map((r) => ({ ...r })) }))
  let minted = 0

  const wire = (j: Fixture): Job => ({
    id: j.id, name: j.name, enabled: j.enabled, message: j.message,
    kind: j.kind, expr: j.expr,
    /* Omitted rather than null when there is no such instant. A live gateway
       sends null here (raven/rpc/methods/console.py) and the contract declares
       neither the null nor a nullable type, which is why this used to be built
       behind a cast; both readings are "nothing to show" to every reader of the
       field (features/cron/source.ts tests it for truth, and fmtStamp takes
       null and undefined alike). */
    next_run_at_ms: j.enabled && j.next ? env.now() + j.next : undefined,
    last_run_at_ms: j.last ? env.now() - j.last : undefined,
    last_status: j.status,
  })

  const find = (id: string | undefined): Fixture | undefined => jobs.find((j) => j.id === id)

  return {
    fixtures: {
      'cron.list': () => ({ jobs: jobs.map(wire) }),
      'cron.runs': (p) => ({
        runs: (find(p.id) || { runs: [] }).runs.map((r): Run => ({
          at_ms: env.now() - r.ago, ok: r.ok, preview: r.note,
        })),
        session_id: `cron:${p.id}`,
      }),
      'cron.set_enabled': (p) => {
        const j = find(p.id)
        if (j) j.enabled = !!p.enabled
        return { enabled: !!p.enabled }
      },
      'cron.delete': (p) => {
        const at = jobs.findIndex((j) => j.id === p.id)
        if (at >= 0) jobs.splice(at, 1)
        return { deleted: true }
      },
      'cron.save': (p) => {
        const existing = find(p.id)
        if (existing) {
          existing.name = p.name || existing.name
          existing.message = p.message || existing.message
          existing.kind = p.kind || existing.kind
          existing.expr = p.expr || existing.expr
          return { job: wire(existing) }
        }
        minted += 1
        const fresh: Fixture = {
          id: `j${100 + minted}`, name: p.name || '', enabled: true,
          message: p.message || '', kind: p.kind || 'cron', expr: p.expr || '0 8 * * *',
          next: 12 * HOUR, runs: [],
        }
        jobs.push(fresh)
        return { job: wire(fresh) }
      },
      'cron.run_now': () => ({ ok: true }),
    },
  }
}
