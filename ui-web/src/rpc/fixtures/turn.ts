/* The two scripted conversations, and the turn that plays one out.
 *
 * The offline canvas has always had a canned conversation; what changed is
 * where it is plugged in. It used to be a replay engine calling the transcript
 * island's own verbs, so the offline page exercised a second painter and the
 * live pipeline went untested by it. Here
 * the same script is pushed as `event` frames on the subscription the page
 * opened, on the transport's own timer -- so the offline page plays the turn
 * out over time through exactly the stage table the live page uses, and a
 * stage that stops drawing something is visibly broken in both modes.
 *
 * The tables below are the demo's, moved: the prose of two conversations, the
 * tool calls they make, the graph one of them orchestrates, and the selector
 * that decides which of the two a typed message starts.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'
import type { ResultOf, TurnEvent } from '../generated'

/* A tool's arguments as the wire carries them. A script entry gives either the
   whole argument object -- which is what the workspace record and the dag card
   need -- or the one string the card labels the row with. */
type ToolArgs = Extract<TurnEvent, { type: 'tool.start' }>['payload']['arguments']

export interface DeliveryFile {
  path: string
  name: string
  title: string
  size: number
  media_type: string
  description?: string
}

/* The one metadata a script's completions carry, and what the shelf and
   `deliverables.list` read back out of it. A type alias rather than an
   interface on purpose: the contract types `metadata` as an index signature,
   and only a type literal is assignable to one without an assertion. */
type DeliveryMeta = { raven_delivery: { files: DeliveryFile[] } }

/* The two dag frames the graph conversation pushes. Unlike every other kind,
   a dag entry's `p` IS the wire payload, so it is typed as the contract's own
   -- and the pairing of `k` with `p` is what makes each frame build without an
   assertion. */
type DagEntry =
  | { t: 'dag'; k: 'dag.run_started'; p: Extract<TurnEvent, { type: 'dag.run_started' }>['payload'] }
  | { t: 'dag'; k: 'dag.node_updated'; p: Extract<TurnEvent, { type: 'dag.node_updated' }>['payload'] }

/* One entry of a script, in the shorthand the replay used: `t` is the kind,
   `d` the gap in ms since the previous entry, and the rest is the kind's own
   payload -- a union over `t` rather than one bag of optionals, so the fields
   an entry carries are its kind's and `framesOf` builds each frame from a
   narrowed entry. Kept as the script's own shape rather than written out as
   wire frames, because the shape a person edits and the shape the page
   consumes are different jobs -- `framesOf` below translates. */
export type ScriptEvent = { d?: number } & (
  | { t: 'ep' }
  | { t: 'think'; s?: number; x: string }
  | { t: 'say'; x: string }
  | { t: 'answer'; x: string }
  | { t: 't+'; id: number; n: string; a?: string | ToolArgs }
  | { t: 't-'; id: number; r: string; ok?: boolean; ms?: number; diff?: string[]; meta?: DeliveryMeta }
  | DagEntry
  | { t: 'end' }
)

export interface Run {
  key: string
  title: string
  ask: string
  use: { calls: number; in: number; out: number; cost: number; wall: number }
  ev: ScriptEvent[]
  evOk?: ScriptEvent[]
}

const NOKEY = 'Error: web search is not configured (no API key)';

/* What the GTM run writes out, so the products bar in demo mode shows a
   miniature of a real document rather than a placeholder for one. */
const GTM_DOC = `## GTM Agent 赛道对比

抓取自三家官网，2026-08。

| 产品 | 定位 | 核心能力 |
| --- | --- | --- |
| Clay | 数据编排 | 100+ 数据源做线索富化 |
| 11x | 数字销售代表 | 全自动外呼与跟进 |
| Unify | 意图信号 | 网站访客到线索的意图判定 |

### 判断

三家都在把整条链路交给 agent 自动跑，差异在起点：Clay 从数据起，11x 从触达起，
Unify 从信号起。
`;

const GTM_DELIVERY_FILES: DeliveryFile[] = [
  { path:'research/gtm-compare.md', name:'gtm-compare.md', title:'GTM agent 赛道对比', size:1824, media_type:'text/markdown' },
  { path:'research/pricing.csv', name:'pricing.csv', title:'产品定价明细', size:936, media_type:'text/csv' },
  { path:'research/source-notes.pdf', name:'source-notes.pdf', title:'官网摘录', size:88420, media_type:'application/pdf' },
  { path:'research/market-map.xlsx', name:'market-map.xlsx', title:'市场分层', size:24118, media_type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
  { path:'research/brief.docx', name:'brief.docx', title:'研究摘要', size:16820, media_type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document' },
];

const FIX_DELIVERY_FILES: DeliveryFile[] = [
  { path:'internal/db/pool.go', name:'pool.go', title:'连接池修复',
    description:'后台任务使用独立连接池，登录请求增加 2 秒超时。', size:3412, media_type:'text/x-go' },
];

const GTM_FILE_EVENTS: ScriptEvent[] = [
  { t:'t+', d:180, id:6, n:'write_file', a:{ path:'research/pricing.csv', content:'product,plan,price\nClay,Launch,167\n11x,Digital Worker,custom\nUnify,Growth,custom\n' } },
  { t:'t-', d:220, id:6, ok:true, r:'wrote research/pricing.csv (4 lines)', ms:220 },
  { t:'t+', d:160, id:7, n:'write_file', a:{ path:'research/market-map.json', content:'{"leaders":["Clay","11x","Unify"],"reviewed":"2026-08"}\n' } },
  { t:'t-', d:190, id:7, ok:true, r:'wrote research/market-map.json (1 line)', ms:190 },
  { t:'t+', d:150, id:8, n:'edit_file', a:{ path:'research/README.md', old_text:'## Status\nDraft\n', new_text:'## Status\nResearch complete\n' } },
  { t:'t-', d:180, id:8, ok:true, r:'updated research/README.md', ms:180 },
  { t:'t+', d:150, id:9, n:'edit_file', a:{ path:'research/sources.md', old_text:'- Clay\n- 11x\n', new_text:'- Clay\n- 11x\n- Unify\n' } },
  { t:'t-', d:180, id:9, ok:true, r:'updated research/sources.md', ms:180 },
  { t:'t+', d:260, id:10, n:'write_file', a:{ path:'research/gtm-compare.md', content: GTM_DOC } },
  { t:'t-', d:340, id:10, ok:true, r:'wrote research/gtm-compare.md (18 lines)', ms:340 },
];

const ANSWER_GTM = `## GTM Agent 赛道速览

抓取了三家代表产品的官网。**共同点是把「找线索 → 判断意图 → 个性化触达」整条链路交给 agent 自动跑**，而不再只卖数据或模板。

| 产品 | 定位 | 核心能力 | 融资 |
|---|---|---|---|
| Clay | 数据编排 + 触达 | 100+ 数据源瀑布补全、AI 写话术 | Series B · $62M |
| 11x.ai | 数字销售代表 | 全自动 SDR，自主排程与跟进 | Series B · $50M |
| Unify | 意图信号驱动 | 访客识别、意图评分、自动序列 | Series A · $12M |

### 三点判断

- **从「工具」转向「代理」**：过去卖数据和模板，现在卖「替你做完」。
- **数据仍是护城河**：Clay 的优势不在模型，在数据源编排。
- **定价从按座位转向按结果**：11x 已按「生成的会议数」计费。`;

/* The graph the research conversation orchestrates: three reads in parallel,
   fanning into one comparison. Written the way the model writes it -- ids,
   dependencies, a prompt template per node and the inputs it names -- because
   that is what the transcript's dag card renders. Every placeholder here resolves
   against a node or an input that exists; a fixture whose template referred to
   nothing would draw a card the real tool would have rejected. */
const DAG_RUN = '20260821T004119Z-4c1d8ea2';

/* One node as the dispatching call carries it, keyed by `id`. The snapshot
   `dag.get` answers calls the same field `node` and forbids anything it does
   not declare (rpc-schema/openrpc.json's DagSnapshotNode), so the two are
   mapped rather than spread -- the spread this replaced sent `id` alongside
   `node`, a property the contract does not allow and nothing reads
   (features/dag/nodes.ts reads `node`). */
interface DagNode {
  id: string
  subagent: string
  depends_on: string[]
  prompt_template: string
  inputs: NonNullable<ResultOf<'dag.get'>['run']['files'][number]['inputs']>
}

const DAG_GTM: { nodes: DagNode[]; background: boolean } = {
  nodes: [
    { id: 'read_clay', subagent: 'Raven-X', depends_on: [],
      prompt_template: '\u7ec6\u8bfb {{ inputs.page }}\uff0c\u6309\u300c\u5b9a\u4f4d / \u6838\u5fc3\u80fd\u529b / \u5b9a\u4ef7 / \u96c6\u6210\u300d\u56db\u6817\u51fa\u7ed3\u6784\u5316\u6458\u8981\uff0c\u6bcf\u6817\u4e24\u4e09\u53e5\uff0c\u5e26\u539f\u6587\u51fa\u5904\u3002',
      inputs: { page: { file: 'research/clay.com.md' } } },
    { id: 'read_11x', subagent: 'Raven-X', depends_on: [],
      prompt_template: '\u7ec6\u8bfb {{ inputs.page }}\uff0c\u6309\u540c\u4e00\u56db\u6817\u51fa\u7ed3\u6784\u5316\u6458\u8981\uff0c\u53e3\u5f84\u8ddf {{ inputs.rubric }} \u5bf9\u9f50\u3002',
      inputs: { page: { file: 'research/11x.ai.md' }, rubric: 'read_clay' } },
    { id: 'read_unify', subagent: 'Raven-X', depends_on: [],
      prompt_template: '\u7ec6\u8bfb {{ inputs.page }}\uff0c\u6309\u540c\u4e00\u56db\u6817\u51fa\u7ed3\u6784\u5316\u6458\u8981\u3002',
      inputs: { page: { file: 'research/unifygtm.com.md' } } },
    { id: 'compare', subagent: 'raven', depends_on: ['read_clay', 'read_11x', 'read_unify'],
      prompt_template: '\u628a {{ read_clay.output }}\u3001{{ read_11x.output }}\u3001{{ read_unify.output }} \u5408\u6210\u4e00\u5f20\u5bf9\u6bd4\u8868\uff0c\u56db\u6817\u5bf9\u9f50\u3002\u53e3\u5f84\u4e0d\u4e00\u81f4\u7684\u5730\u65b9\u5355\u72ec\u5217\u4e00\u884c\u8bf4\u660e\uff0c\u4e0d\u8981\u62b9\u5e73\u3002\u8bed\u6c14\u53c2\u8003 {{ inputs.voice }}\u3002',
      inputs: { voice: { file: 'docs/style/report.md' }, audience: '\u8981\u505a\u9009\u578b\u51b3\u7b56\u7684\u589e\u957f\u8d1f\u8d23\u4eba' } },
  ],
  background: true,
};

const RUNS: Record<string, Run> = {
  gtm: {
    key: 'gtm', title: 'GTM agent 市场调研',
    ask: '调研一下市场上做 GTM agent 的产品',
    use: { calls: 4, in: 14226, out: 3180, cost: 0.021, wall: 41000 },
    /* Web search unconfigured: searches fail; fall back to fetching official sites directly */
    ev: [
      { t:'ep' },
      { t:'think', d:900, s:7, x:'用户想了解 GTM（Go-To-Market）agent 赛道。要覆盖四块：代表性公司、产品能力、融资、趋势判断。先并行搜几路关键词，再抓官网补细节，最后交叉核对。' },
      { t:'say', d:700, x:'我来调研市场上做 GTM Agent 的产品，先并行搜几路关键词。' },
      { t:'t+', d:340, id:1, n:'web_search', a:'GTM agent AI go-to-market automation 2026' },
      { t:'t-', d:640, id:1, ok:false, r:NOKEY, ms:0 },
      { t:'t+', d:110, id:2, n:'web_search', a:'best GTM AI agents Clay Apollo 11x' },
      { t:'t-', d:560, id:2, ok:false, r:NOKEY, ms:0 },
      { t:'ep', d:420 },
      { t:'think', d:800, s:6, x:'搜索能力没启用。改用 web_fetch 直接抓官网——信息密度更高，而且可引用来源。' },
      { t:'say', d:600, x:'网页搜索还没配置，我改用直接抓取官网的方式。' },
      { t:'t+', d:300, id:3, n:'web_fetch', a:'https://www.clay.com' },
      { t:'t-', d:1500, id:3, ok:true, r:'{"status":200,"extractor":"jina-reader","length":5000}', ms:1500 },
      { t:'t+', d:140, id:4, n:'web_fetch', a:'https://www.11x.ai' },
      { t:'t-', d:1150, id:4, ok:true, r:'{"status":200,"extractor":"jina-reader","length":4820}', ms:1150 },
      { t:'t+', d:140, id:5, n:'web_fetch', a:'https://unifygtm.com' },
      { t:'t-', d:900, id:5, ok:true, r:'{"status":200,"extractor":"jina-reader","length":3960}', ms:900 },
      { t:'ep', d:420 },
      { t:'think', d:850, s:5, x:'三家数据够了，整理成对比表，再给趋势判断。要标注搜索没跑，融资数字可能滞后。' },
      ...GTM_FILE_EVENTS,
      { t:'t+', d:120, id:11, n:'deliver_files', a:{ files:[{ path:'research/gtm-compare.md' }] } },
      { t:'t-', d:120, id:11, ok:true, r:'Delivered 1 file: gtm-compare.md', ms:120,
        meta:{ raven_delivery:{ files:GTM_DELIVERY_FILES } } },
      { t:'answer', d:500, x: ANSWER_GTM + `

### 一处存疑

本轮**网页搜索未启用**，以上只基于三家官网的公开信息，融资数字可能滞后。在「能力」里配好网页搜索后，我可以再跑一轮交叉验证。` },
      { t:'end' }
    ],
    /* Web search configured: searches succeed, coverage is wider, findings can be cross-checked */
    evOk: [
      { t:'ep' },
      { t:'think', d:900, s:7, x:'GTM agent 赛道。先并行搜三路关键词拿到候选名单，再挑代表性的抓官网核对细节，最后交叉验证融资数字。' },
      { t:'say', d:700, x:'我来调研市场上做 GTM Agent 的产品，先并行搜几路关键词。' },
      { t:'t+', d:340, id:1, n:'web_search', a:'GTM agent AI go-to-market automation 2026' },
      { t:'t-', d:900, id:1, ok:true, r:'18 条结果 · clay.com / 11x.ai / unifygtm.com / apollo.io …', ms:900 },
      { t:'t+', d:120, id:2, n:'web_search', a:'GTM agent funding round 2026' },
      { t:'t-', d:780, id:2, ok:true, r:'11 条结果 · crunchbase / techcrunch / saastr', ms:780 },
      { t:'ep', d:400 },
      { t:'think', d:800, s:6, x:'名单齐了。抓两家官网补产品细节，融资用检索结果交叉核对。' },
      { t:'say', d:600, x:'名单拿到了，我抓官网补产品细节，融资数字用检索结果交叉核对。' },
      { t:'t+', d:300, id:3, n:'web_fetch', a:'https://www.clay.com' },
      { t:'t-', d:1400, id:3, ok:true, r:'{"status":200,"extractor":"jina-reader","length":5000}', ms:1400 },
      { t:'t+', d:140, id:4, n:'web_fetch', a:'https://www.11x.ai' },
      { t:'t-', d:1100, id:4, ok:true, r:'{"status":200,"extractor":"jina-reader","length":4820}', ms:1100 },
      { t:'ep', d:400 },
      { t:'think', d:900, s:7, x:'三家各自的细节不是一个人读得完的量。拆成三路并行读，各出一份结构化摘要，再汇到一个节点里对表——这就是一张 dag。' },
      { t:'say', d:700, x:'我把三家分头交给 subagent 细读，最后汇成一张对比表。' },
      /* The one dag call on this canvas, and its `a` is the argument object
       rather than a label string: the card is built from the arguments, so a
       label would draw three boxes and no edges -- the shape this card exists
       to stop showing. */
      { t:'t+', d:300, id:5, n:'run_subagent_dag', a: DAG_GTM },
      { t:'dag', d:120, k:'dag.run_started', p:{ run_id: DAG_RUN,
        nodes: DAG_GTM.nodes.map((n) => ({ id: n.id, subagent: n.subagent, depends_on: n.depends_on })) } },
      { t:'dag', d:200, k:'dag.node_updated', p:{ run_id: DAG_RUN, node:'read_clay', status:'running', started_at: 1000 } },
      { t:'dag', d:40,  k:'dag.node_updated', p:{ run_id: DAG_RUN, node:'read_11x', status:'running', started_at: 1000 } },
      { t:'dag', d:40,  k:'dag.node_updated', p:{ run_id: DAG_RUN, node:'read_unify', status:'running', started_at: 1000 } },
      { t:'dag', d:700, k:'dag.node_updated', p:{ run_id: DAG_RUN, node:'read_clay', status:'completed', ended_at: 5200 } },
      { t:'dag', d:260, k:'dag.node_updated', p:{ run_id: DAG_RUN, node:'read_unify', status:'completed', ended_at: 6100 } },
      { t:'dag', d:180, k:'dag.node_updated', p:{ run_id: DAG_RUN, node:'read_11x', status:'completed', ended_at: 6900 } },
      { t:'dag', d:120, k:'dag.node_updated', p:{ run_id: DAG_RUN, node:'compare', status:'running', started_at: 6900 } },
      { t:'dag', d:900, k:'dag.node_updated', p:{ run_id: DAG_RUN, node:'compare', status:'completed', ended_at: 10400 } },
      { t:'t-', d:200, id:5, ok:true, ms:2600,
        r:`DAG ${DAG_RUN}: 4 个节点全部完成` },
      { t:'ep', d:400 },
      { t:'think', d:850, s:5, x:'官网与检索结果对得上，可以出表。' },
      ...GTM_FILE_EVENTS,
      { t:'t+', d:120, id:11, n:'deliver_files', a:{ files:[{ path:'research/gtm-compare.md' }] } },
      { t:'t-', d:120, id:11, ok:true, r:'Delivered 1 file: gtm-compare.md', ms:120,
        meta:{ raven_delivery:{ files:GTM_DELIVERY_FILES } } },
      { t:'answer', d:500, x: ANSWER_GTM + `

### 可信度

融资数字由**检索结果与官网双向核对**，两处一致。搜索一并覆盖了 Apollo、Regie 等次级玩家，未进表是因为主业不在 agent。` },
      { t:'end' }
    ]
  },

  fix: {
    key: 'fix', title: '修复登录偶发超时',
    ask: '登录接口偶发超时，帮我定位并修掉',
    use: { calls: 7, in: 38104, out: 5202, cost: 0.058, wall: 96000 },
    ev: [
      { t:'ep' },
      { t:'think', d:900, s:8, x:'偶发超时的常见成因：连接池耗尽、下游依赖抖动、锁竞争。先定位代码入口，日志量大就交给 subagent 并行扫。' },
      { t:'say', d:700, x:'我先看登录链路的代码，同时派两个 subagent 分头查日志和数据库。' },
      { t:'t+', d:300, id:1, n:'grep', a:'func Login' },
      { t:'t-', d:480, id:1, ok:true, r:'internal/handler/auth.go:42 · internal/service/session.go:118', ms:480 },
      { t:'t+', d:130, id:2, n:'read_file', a:'internal/service/session.go' },
      { t:'t-', d:390, id:2, ok:true, r:'package service · 214 行', ms:390 },
      { t:'t+', d:130, id:3, n:'spawn', a:'log-analyst · 扫 7 天网关日志' },
      { t:'t+', d:90,  id:4, n:'spawn', a:'db-inspector · 检查连接池与慢查询' },
      { t:'t-', d:2500, id:3, ok:true, r:'超时集中在每小时 00-03 分，p99 从 180ms 跳到 12s，与定时任务重合', ms:2500 },
      { t:'t-', d:420, id:4, ok:true, r:'max_open_conns=10；定时任务批量写入时占满连接池', ms:2900 },
      { t:'ep', d:420 },
      { t:'think', d:1000, s:9, x:'两条线索对上了：批量任务占满 10 个连接，登录请求排队等连接直到网关超时。修法是给后台任务独立连接池，并让请求路径快速失败。' },
      { t:'say', d:700, x:'定位到了：整点的批量任务把连接池占满，登录请求在排队等连接。我来改。' },
      { t:'t+', d:300, id:5, n:'edit_file', a:'internal/db/pool.go' },
      { t:'t-', d:620, id:5, ok:true, r:'+8 -2', ms:620,
        diff:['@@ -14,7 +14,21 @@ func NewPool(cfg Config) (*Pool, error) {',
              '-\tdb.SetMaxOpenConns(10)',
              '-\tdb.SetMaxIdleConns(2)',
              '+\t// Requests and background jobs no longer share a pool: a batch',
              '+\t// write used to hold all 10 connections and login requests',
              '+\t// queued behind it until the gateway timed out.',
              '+\tdb.SetMaxOpenConns(25)',
              '+\tdb.SetMaxIdleConns(8)',
              '+\tbg := db.Clone()',
              '+\tbg.SetMaxOpenConns(4)'] },
      { t:'t+', d:140, id:6, n:'exec', a:'go test ./internal/... -run TestLogin -count=3' },
      { t:'t-', d:2100, id:6, ok:true, r:'ok internal/service 4.812s · 3 runs, 0 failures', ms:2100 },
      { t:'ep', d:300 },
      { t:'t+', d:200, id:7, n:'list_dir', a:'internal/db' },
      { t:'t-', d:300, id:7, ok:true, ms:300,
        r:'pool.go\npool_test.go\nmigrate.go\ntx.go\nconn.go\nmetrics.go\nretry.go\nREADME.md' },
      { t:'ep', d:150 },
      { t:'t+', d:150, id:8, n:'read_file', a:'internal/db/pool_test.go' },
      { t:'t-', d:250, id:8, ok:true, r:'package db · 3 处断言引用了旧的连接上限', ms:250 },
      { t:'ep', d:150 },
      { t:'t+', d:150, id:9, n:'exec', a:'go vet ./internal/db/...' },
      { t:'t-', d:400, id:9, ok:true, r:'', ms:400 },
      { t:'ep', d:320 },
      { t:'think', d:700, s:4, x:'测试通过。把根因、改动、验证写清楚，并给出后续建议。' },
      { t:'t+', d:120, id:10, n:'deliver_files', a:{ files:[{ path:'internal/db/pool.go' }] } },
      { t:'t-', d:120, id:10, ok:true, r:'Delivered 1 file: pool.go', ms:120,
        meta:{ raven_delivery:{ files:FIX_DELIVERY_FILES } } },
      { t:'answer', d:500, x:`## 根因

整点的批量同步任务和登录请求**共用同一个连接池**（\`max_open_conns=10\`）。任务一跑就占满连接，登录请求卡在获取连接处，一直等到网关 15s 超时。

这解释了为什么是「偶发」——只在每小时 00-03 分复现。

## 改动

- 后台任务改用**独立连接池**（上限 4），与请求路径隔离。
- 请求路径获取连接加 **2s 上限**，拿不到就快速失败并计入指标，而不是干等到网关超时。
- 主池上限 10 → 25（实测峰值并发 18）。

## 验证

\`\`\`
go test ./internal/... -run TestLogin -count=3   →  3 runs, 0 failures
\`\`\`

建议再观察一个整点周期。要我顺手加一条「p99 超过 1s」的告警吗？` },
      { t:'end' }
    ]
  }
};

/* Which of the two conversations a typed message starts. The regular
   expression is the selector itself and moved unchanged: these are the words
   that mean "something is broken", and anything else is taken as research. */
const pickRun = (s: string): Run => (/超时|timeout|登录|bug|修|fix|报错|定位|回调/.test(s) ? RUNS.fix! : RUNS.gtm!)

/* Which fork of the research conversation runs: with web search configured the
   searches succeed and the findings can be cross-checked, without it they fail
   and the model falls back to fetching the official sites. */
const eventsFor = (run: Run, websearchOn: boolean): ScriptEvent[] =>
  (run.key === 'gtm' && websearchOn && run.evOk) ? run.evOk : run.ev

/* The live frame carries both fields, so a script entry that gives the label
   string becomes the argument the tool would have been called with plus the
   label. */
function argumentsOf(name: string, a: string | ToolArgs | undefined): ToolArgs {
  if (a && typeof a === 'object') return a
  const s = String(a == null ? '' : a)
  if (name === 'exec') return { command: s }
  if (name === 'web_fetch') return { url: s }
  if (name === 'web_search') return { query: s }
  if (name === 'spawn') return { label: s }
  return { path: s }
}

/* One arm per kind, because `{ type: e.k, payload: e.p }` off the union pairs
   either kind with either payload -- which is not a frame the contract
   declares, and tsc is right to refuse it. */
const dagFrame = (e: DagEntry): TurnEvent =>
  (e.k === 'dag.run_started' ? { type: e.k, payload: e.p } : { type: e.k, payload: e.p })

const TYPE_MS = 24
const CHUNK = /[\s\S]{1,26}/g

interface Frame {
  after: number
  event: TurnEvent
}

/* The script as frames, each with the gap that precedes it. The answer is
   chunked the way the replay typed it, because that is what the transcript's
   streaming path is for: one `token.delta` per chunk rather than one for the
   whole answer. */
function framesOf(run: Run, websearchOn: boolean, turnId: string): Frame[] {
  const frames: Frame[] = []
  let episode = 0
  for (const e of eventsFor(run, websearchOn)) {
    const after = e.d || 0
    if (e.t === 'ep') {
      episode += 1
      frames.push({ after, event: { type: 'episode.start', payload: { index: episode } } })
    } else if (e.t === 'think') {
      frames.push({ after, event: { type: 'thinking.delta', payload: { text: e.x } } })
    } else if (e.t === 'say') {
      frames.push({ after, event: { type: 'token.delta', payload: { text: e.x } } })
    } else if (e.t === 't+') {
      frames.push({ after, event: { type: 'tool.start', payload: {
        tool_call_id: String(e.id), name: e.n, arguments: argumentsOf(e.n, e.a),
        display: typeof e.a === 'string' ? e.a : null } } })
    } else if (e.t === 't-') {
      frames.push({ after, event: { type: 'tool.complete', payload: {
        tool_call_id: String(e.id), result_preview: e.r, truncated: false,
        ok: e.ok !== false, ...(e.meta ? { metadata: e.meta } : {}),
        ...(e.diff ? { diff: e.diff.join('\n') } : {}) } } })
    } else if (e.t === 'dag') {
      frames.push({ after, event: dagFrame(e) })
    } else if (e.t === 'answer') {
      const parts = e.x.match(CHUNK) || []
      parts.forEach((part, i) => {
        frames.push({ after: i === 0 ? after : TYPE_MS, event: { type: 'token.delta', payload: { text: part } } })
      })
    } else if (e.t === 'end') {
      frames.push({ after: 300, event: { type: 'message.complete', payload: {
        turn_id: turnId,
        duration_ms: run.use.wall,
        usage: { prompt_tokens: run.use.in, completion_tokens: run.use.out,
          total_tokens: run.use.in + run.use.out, cost_usd: run.use.cost,
          context_used: run.use.in + run.use.out, context_max: 200000 },
      } } })
    }
  }
  return frames
}

/* The same script read back as a stored conversation: what `session.resume`
   answers for a session whose turn already ran. The replay used to paint this
   by running the script through the island at zero delay; a page that reads
   its history off the wire gets it as messages instead, which is the shape a
   real transcript on disk has. */
function historyOf(run: Run, websearchOn: boolean, at: number): ResultOf<'session.resume'>['messages'] {
  const messages: ResultOf<'session.resume'>['messages'] = [
    { role: 'user', text: run.ask, timestamp: String(at) },
  ]
  let think = ''
  let say = ''
  const pending: Array<{ id: string; name: string; args: string }> = []
  const flush = (): void => {
    if (!pending.length && !say && !think) return
    messages.push({ role: 'assistant', text: say, reasoning_content: think,
      tool_calls: pending.map((c) => ({ id: c.id, name: c.name, arguments: c.args })), timestamp: String(at) })
    think = ''
    say = ''
    pending.length = 0
  }
  for (const e of eventsFor(run, websearchOn)) {
    if (e.t === 'think') think += e.x
    else if (e.t === 'say') say += e.x
    else if (e.t === 't+') {
      pending.push({ id: String(e.id), name: e.n,
        args: JSON.stringify(argumentsOf(e.n, e.a)) })
    } else if (e.t === 't-') {
      flush()
      messages.push({ role: 'tool', name: nameOfCall(run, websearchOn, String(e.id)),
        tool_call_id: String(e.id), text: e.r, timestamp: String(at) })
    } else if (e.t === 'answer') {
      flush()
      messages.push({ role: 'assistant', text: e.x, timestamp: String(at), duration_ms: run.use.wall })
    }
  }
  flush()
  return messages
}

const nameOfCall = (run: Run, websearchOn: boolean, id: string): string => {
  for (const e of eventsFor(run, websearchOn)) {
    if (e.t === 't+' && String(e.id) === id) return e.n
  }
  return ''
}

/** The first line of what a script answers, which is what a finished turn
    leaves on its conversation's row. */
function previewOf(run: Run, websearchOn: boolean): string {
  const answers = eventsFor(run, websearchOn).flatMap((e) => (e.t === 'answer' ? [e.x] : []))
  const last = answers[answers.length - 1]
  return (last || '').trim().split('\n')[0]!.slice(0, 60)
}

/** What the canvas has to know about the conversation a turn runs in. */
export interface TurnHost {
  /** The script a listed session replayed, by session id. */
  runOf(sessionId: string): Run | null
  /** Remember which script a session is running, for its resume. */
  setRun(sessionId: string, key: string): void
  /** What the row's preview says once the turn is over. */
  finished(sessionId: string, preview: string): void
}

export interface TurnFixture {
  fixtures: Fixtures
  /** The stored conversation for a session, for ./sessions.ts to answer with. */
  history(sessionId: string): ResultOf<'session.resume'>['messages']
  /** The delivered files of a session's turns, same. */
  deliveries(sessionId: string): DeliveryFile[]
}

export function createTurn(env: FixtureEnv, host: TurnHost, websearchOn: () => boolean): TurnFixture {
  /* One subscription per session, which is what the page opens and what every
     frame names. Held both ways so a send can find the stream to push on. */
  const subs = new Map<string, string>()
  /* The turn each conversation is playing, and the ones a cancel stopped: a
     frame scheduled before the stop must not arrive after it, and a cancel is
     one conversation's, never the page's. */
  const playing = new Map<string, string>()
  const cancelled = new Set<string>()
  let seq = 0

  const filesOf = (run: Run): DeliveryFile[] => {
    const files: DeliveryFile[] = []
    for (const e of eventsFor(run, websearchOn())) {
      if (e.t === 't-' && e.meta) files.push(...e.meta.raven_delivery.files)
    }
    return files
  }

  const deliveriesOf = (sessionId: string): DeliveryFile[] => {
    const run = host.runOf(sessionId)
    return run ? filesOf(run) : []
  }

  const play = (sessionKey: string, run: Run, turnId: string): void => {
    const subscription = subs.get(sessionKey)
    if (!subscription) return
    playing.set(sessionKey, turnId)
    let at = 0
    env.emit('event', { subscription_id: subscription, event: { type: 'turn.started', payload: { turn_id: turnId } } })
    for (const frame of framesOf(run, websearchOn(), turnId)) {
      at += frame.after
      env.schedule(at, () => {
        if (cancelled.has(turnId)) return
        env.emit('event', { subscription_id: subscription, event: frame.event })
      })
    }
    env.schedule(at, () => {
      if (cancelled.has(turnId)) return
      playing.delete(sessionKey)
      host.finished(sessionKey, previewOf(run, websearchOn()))
    })
  }

  return {
    history: (sessionId) => {
      const run = host.runOf(sessionId)
      return run ? historyOf(run, websearchOn(), Math.floor(env.now() / 1000)) : []
    },
    deliveries: (sessionId) => deliveriesOf(sessionId),
    fixtures: {
      'turn.subscribe': (p) => {
        seq += 1
        const id = `sub-${seq}`
        subs.set(p.session_key, id)
        return { subscription_id: id, running: false }
      },
      'turn.unsubscribe': (p) => {
        for (const [key, id] of subs) if (id === p.subscription_id) subs.delete(key)
        return { unsubscribed: true }
      },
      'turn.send': (p) => {
        seq += 1
        const turnId = `turn-${seq}`
        const key = p.session_key
        /* A turn addressed to one sub-agent instance is that instance's own,
           and only `?desk-demo=1` has instances to address -- so the canvas
           overrides this method rather than the script answering for it. */
        const run = host.runOf(key) || pickRun(p.content || '')
        host.setRun(key, run.key)
        play(key, run, turnId)
        return { turn_id: turnId, accepted: true, naming: false }
      },
      'turn.cancel': (p) => {
        /* Only this conversation's turn: the script stops where the reader
           stopped it, and another conversation's still plays out. */
        const turnId = playing.get(p.session_key)
        if (turnId) { cancelled.add(turnId); playing.delete(p.session_key) }
        return { cancelled: true }
      },
      /* Only what this conversation's own turns delivered: a shelf filled from
         another conversation's script is a file the reader never made. */
      'deliverables.list': (p) => ({
        files: deliveriesOf(p.session_key || '').map((f) => ({
          path: f.path, name: f.name, title: f.title, size: f.size, media_type: f.media_type,
          ...(f.description ? { description: f.description } : {}),
          download_path: `/files/${f.path}`, created_at: String(Math.floor(env.now() / 1000)), missing: false,
        })),
      }),
      /* The graph read back off disk, which is what a card restored from
         history draws from: the same four nodes the call's arguments named,
         each reported finished. */
      'dag.get': (p) => ({
        run: {
          run_id: p.run_id, dir: `~/work/raven/.raven/dag/${p.run_id}`, finalized: true,
          files: DAG_GTM.nodes.map((n) => ({
            node: n.id, subagent: n.subagent, depends_on: n.depends_on,
            prompt_template: n.prompt_template, inputs: n.inputs,
            status: 'completed' as const,
            started_at: env.now() - 40000, ended_at: env.now() - 30000,
          })),
          summary: { total: DAG_GTM.nodes.length, completed: DAG_GTM.nodes.length },
        },
      }),
      'dag.node': (p) => ({
        node: {
          run_id: p.run_id, node: p.node, status: 'completed',
          output_chars: 0, output_truncated: false,
        },
      }),
    },
  }
}

export { RUNS, DAG_RUN, DAG_GTM, GTM_DOC }
