/* The conversations this install has, and what a reader can do to one.
 *
 * The ten rows the offline canvas has always listed, now as `session.list`
 * sends them: the stamp on a row is computed from the injected clock rather
 * than written out as prose, which is the one visible difference the fixture
 * move makes -- a row that said "yesterday 19:00" now says whatever the rail's
 * own `whenLabel` makes of an instant that far back.
 *
 * Two of the rows carry a scripted conversation (./turn.ts), which is what
 * their `session.resume` answers with.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf } from '../generated'
import type { TurnFixture } from './turn'

type Listed = ResultOf<'session.list'>['sessions'][number]
type InitInfo = ResultOf<'session.create'>['info']

interface Fixture {
  id: string
  /** How far back this row's last activity is, in ms before `now()`. */
  ago: number
  title: string
  last: string
  /** Which scripted conversation it replays, if any. */
  run: string | null
  pin?: boolean
  from?: string
  /** The folder this conversation was started in, when it was pinned to one. */
  workdir?: string
}

const MIN = 60000
const HOUR = 3600000
const DAY = 86400000
const H2 = 2 * HOUR
const H4 = 4 * HOUR + 22 * MIN
const H5 = 5 * HOUR + 43 * MIN
const H6 = 6 * HOUR + 24 * MIN
const D1 = DAY + 5 * HOUR
const D1E = DAY + 16 * HOUR
const D4 = 4 * DAY
const D9 = 9 * DAY

const SESSION_FIXTURES: Fixture[] = [
  { id:'a', ago: H2, title:'GTM agent 市场调研', last:'抓取了三家代表产品的官网，出了对比表', run:'gtm', pin:false },
  { id:'b', ago: H4, workdir:'/home/dev/checkout', title:'修复登录偶发超时',   last:'3 runs, 0 failures · 已改连接池隔离',   run:'fix', pin:true },
  { id:'g', ago: H5, title:'重构支付回调',       last:'出错：找不到模块 stripe',              run:null },
  { id:'c', ago: D1, title:'整理本周迭代进度',   last:'还没开始',                            run:null },
  { id:'h', ago: D1, title:'扫一遍依赖安全告警', last:'运行中 · 已查 12 个包',               run:null },
  { id:'d', ago: D4, title:'把 CSV 导入 Notion', last:'还没开始',                            run:null },
  { id:'f', ago: D9, title:'给 README 加安装说明', last:'已导出 Markdown',                   run:null },
  /* Sessions a schedule produced. They get their own group because they
     arrive while you are away -- mixed into today's they read as things you
     did. Title is the job; the time badge tells the runs apart, so repeating
     it in the title would just eat the width the title needs. */
  { id:'k1', ago: H6, title:'昨日错误日志汇总', last:'3 类错误 · 支付回调占 68%',
    run:null, from:'cron' },
  { id:'k2', ago: D1, title:'竞品动态', last:'网页搜索未配置，只抓到 1 家',
    run:null, from:'cron' },
  { id:'k3', ago: D1E, title:'昨日错误日志汇总', last:'2 类错误',
    run:null, from:'cron' }
];

/* The one stored conversation that is not a script: a turn that failed, kept
   because a list of ten rows where the only openable ones are the two that
   replay reads as a broken page. Its own words, which is why they are here
   rather than worded from a catalogue key -- the failure is this
   conversation's content, not a notice the runtime wrote. */
const STORED: Record<string, ResultOf<'session.resume'>['messages']> = {
  g: [
    { role: 'user', text: '把支付回调那块拆成两个 handler' },
    { role: 'assistant', text: '找不到模块 stripe（internal/pay/callback.go:12）\n\n装上依赖或改用内置 http 客户端后重试。' },
  ],
}

/* What every conversation's banner says. One model, one window, one working
   directory: the canvas has no engine behind it, so the bundle is the shape of
   a real one rather than a reading of anything. */
const info = (title: string): InitInfo => ({
  model: 'claude-fable-5', model_id: 'claude-fable-5', provider: 'anthropic',
  context_window: 200000, lazy: false, skills: {}, tools: {},
  usage: { input: 0, output: 0, cost_usd: 0, calls: 0, context_max: 200000, context_used: 0, context_percent: 0 },
  version: '0.1.0', cwd: '~/work/raven', mcp_servers: [], title,
})

export interface SessionsFixture {
  fixtures: Fixtures
  /** Which script a session runs, which ./turn.ts asks before it plays one. */
  runOf(id: string): string | null
  setRun(id: string, key: string): void
  /** The preview a finished turn leaves on the row. */
  setPreview(id: string, preview: string): void
}

export function createSessions(env: FixtureEnv, turn: () => TurnFixture): SessionsFixture {
  /* The rows, held as state: a delete, a rename, a pin and an archive all
     change what the next `session.list` answers, which is what made the
     offline canvas explorable and what the fixture library has to keep. */
  const rows = SESSION_FIXTURES.map((s) => ({ ...s }))
  let minted = 0

  const wire = (s: Fixture): Listed => ({
    id: s.id,
    title: s.title,
    preview: s.last,
    last_message_preview: s.last,
    message_count: s.run ? 4 : 1,
    started_at: Math.floor((env.now() - s.ago - HOUR) / 1000),
    updated_at: Math.floor((env.now() - s.ago) / 1000),
    ...(s.from ? { source: s.from } : {}),
    ...(s.workdir ? { workdir: s.workdir } : {}),
    pinned: !!s.pin,
  })

  const archived: Fixture[] = []
  const find = (id: string | undefined): Fixture | undefined => rows.find((s) => s.id === id)
  const drop = (id: string | undefined): void => {
    const at = rows.findIndex((s) => s.id === id)
    if (at >= 0) rows.splice(at, 1)
  }

  return {
    runOf: (id) => find(id)?.run ?? null,
    setRun: (id, key) => { const s = find(id); if (s) s.run = key },
    setPreview: (id, preview) => { const s = find(id); if (s) s.last = preview },
    fixtures: {
      'session.list': (p) => ({ sessions: (p.archived ? archived : rows).map(wire) }),
      'session.create': () => {
        minted += 1
        const s: Fixture = { id: `n${minted}`, ago: 0, title: '', last: '', run: null }
        rows.unshift(s)
        return { session_id: s.id, info: info('') }
      },
      'session.resume': (p) => {
        const id = p.session_id || ''
        const s = find(id)
        return {
          session_id: id,
          info: info((s && s.title) || ''),
          messages: s && s.run ? turn().history(id) : STORED[id] || [],
        }
      },
      'session.title': (p) => {
        const s = find(p.session_id)
        if (s && p.title) s.title = p.title
        return { title: (s && s.title) || '', session_key: p.session_id || '', pending: false }
      },
      'session.pin': (p) => {
        const s = find(p.session_id)
        if (s) s.pin = !!p.pinned
        return { pinned: !!p.pinned, session_key: p.session_id || '', pending: false }
      },
      /* Archiving moves the row to the archived shelf, which `session.list
         {archived: true}` answers from; restoring moves it back. */
      'session.archive': (p) => {
        if (p.archived) {
          const s = find(p.session_id)
          drop(p.session_id)
          if (s) archived.unshift(s)
        } else if (!find(p.session_id)) {
          const at = archived.findIndex((s) => s.id === p.session_id)
          const back = at >= 0 ? archived.splice(at, 1)[0]! : { id: p.session_id, ago: 0, title: '', last: '', run: null }
          rows.unshift(back)
        }
        return { archived: !!p.archived, session_key: p.session_id || '', pending: false }
      },
      /* A delete the canvas really makes: the row goes and the list says the
         transcript went with it, which is the answer the rail reads to decide
         whether the row may leave. */
      'session.delete': (p) => {
        drop(p.session_id)
        return { deleted: p.session_id || '', still_on_disk: false }
      },
      'session.clear': (p) => {
        const s = find(p.session_id)
        if (s) s.run = null
        return { session_id: p.session_id || '', cleared: true }
      },
      'session.compress': (p) => ({
        before_messages: 12, after_messages: 4, before_tokens: 18400, after_tokens: 3100,
        removed: 8, summary: { headline: 'compacted', noop: false },
        info: info((find(p.session_id) || { title: '' }).title),
      }),
      'session.branch': (p) => {
        const from = find(p.session_id)
        minted += 1
        const s: Fixture = { id: `n${minted}`, ago: 0, title: `${(from && from.title) || ''} (branch)`,
          last: '', run: (from && from.run) || null }
        rows.unshift(s)
        return { session_id: s.id, title: s.title, message_count: 2 }
      },
    },
  }
}
