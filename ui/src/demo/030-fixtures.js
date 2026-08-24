/* ══ extension data ═════════════════════════════════════════════════
   Three kinds, split by WHOSE CODE IT IS -- which is exactly what
   decides the operations the user gets:
     技能  method and knowledge, no new action  -> add / toggle / remove
     工具  Raven's own actions, ship with it    -> toggle ONLY
     插件  every action that comes from outside -> install / authorize / remove
   Orthogonal to all three: `reach`, shown as a badge on every row.
   "Built in" does not mean "safe" -- exec ships with Raven and rewrites
   your disk -- so the badge is what answers "where does my data go".   */
/* ── 工具: built in, fixed list, on/off only ───────────────────────── */
const TOOL_GROUPS = [
  { id:'file', label:'gui.toolgrp.file', hint:'gui.toolgrp.file_hint' },
  { id:'run',  label:'gui.toolgrp.run' },
  { id:'net',  label:'gui.toolgrp.net' },
  { id:'ask',  label:'gui.toolgrp.ask' }
];

const TOOLS = [
  { id:'read_file',  name:'读文件',   group:'file', reach:'local', on:true,  one:'读取工作目录里的文件' },
  { id:'write_file', name:'写文件',   group:'file', reach:'local', on:true,  one:'新建或覆盖文件', danger:true },
  { id:'edit_file',  name:'改文件',   group:'file', reach:'local', on:true,  one:'按行修改已有文件', danger:true },
  { id:'list_dir',   name:'列目录',   group:'file', reach:'local', on:true,  one:'看目录结构' },
  { id:'grep',       name:'全文搜索', group:'file', reach:'local', on:true,  one:'在代码和文档里找内容' },
  { id:'exec',       name:'执行命令', group:'run',  reach:'local', on:true,  one:'跑命令、跑测试、看输出', danger:true },
  { id:'spawn',      name:'派子任务', group:'run',  reach:'local', on:true,  one:'把大活拆给多个分身并行做' },
  { id:'web_fetch',  name:'抓网页',   group:'net',  reach:'net',   on:true,  one:'把给定网址读成正文' },
  { id:'deep_research', name:'深度调研', group:'net', reach:'net', on:false, one:'多轮检索加交叉核对，慢且贵' },
  { id:'ask_user',   name:'反问你',   group:'ask',  reach:'local', on:true,  one:'拿不准时停下来问，而不是猜' }
];

/* ── 技能: method only, never a new action ─────────────────────────── */
const SKILLS = [
  { id:'review',  name:'代码审查清单', reach:'local', state:'on',  glyph:'✓', ver:'1.2.0', src:'技能库',
    one:'按你团队的规矩审代码，而不是通用建议', cat:'开发' },
  { id:'writing', name:'中文写作规范', reach:'local', state:'off', glyph:'文', ver:'0.8.1', src:'技能库',
    one:'术语、标点、语气按你的规范来', cat:'内容' },
  { id:'compete', name:'竞品调研方法', reach:'local', state:'add', glyph:'◎', ver:'0.4.0', src:'技能库',
    one:'一套做市场调研的步骤与输出格式', cat:'调研' },
  { id:'meeting', name:'会议纪要格式', reach:'local', state:'add', glyph:'▤', ver:'1.0.3', src:'技能库',
    one:'决议、责任人、截止时间三段式', cat:'办公' },
  { id:'sqlstyle', name:'SQL 规范',   reach:'local', state:'add', glyph:'⌗', ver:'0.6.0', src:'技能库',
    one:'命名、缩进、禁用写法按你们的库来', cat:'开发' }
];

/* ── 插件: every action that comes from outside ────────────────────── */
const PLUGINS = [
  { id:'websearch', name:'网页搜索', reach:'auth', state:'need', glyph:'⌕', ver:'—', src:'serper.dev · MCP',
    one:'让 Raven 查得到网上的实时信息', cat:'上网',
    tools:['web_search','web_news'], perms:['联网'],
    fields:[{ k:'API Key', ph:'从 serper.dev 控制台复制', pw:true }],
    why:'没有它，Raven 只能抓你给出的网址，不能自己找资料。' },
  { id:'github', name:'GitHub', reach:'auth', state:'fail', glyph:'⌥', ver:'1.4.0', src:'github.com/mcp',
    one:'读 issue 与 PR，提交评论', cat:'我的系统', account:'arelchan',
    tools:['list_issues','get_pr','create_comment'], perms:['联网','以你的身份发言'],
    err:'握手失败：token 已过期（HTTP 401）',
    fields:[{ k:'Personal access token', ph:'ghp_…', pw:true }],
    why:'重新生成 token 后填进来即可，权限只需 repo 读取。' },
  { id:'sheets', name:'表格处理', reach:'local', state:'on', glyph:'▦', ver:'0.9.0', src:'raven-sheets',
    one:'读写 CSV 与 Excel，做汇总', cat:'文档',
    tools:['read_sheet','write_sheet'], perms:['读取文件','修改文件'] },
  { id:'notion', name:'Notion', reach:'auth', state:'off', glyph:'◧', ver:'2.1.0', src:'notion.so/mcp',
    one:'把结果写进你的 Notion 库', cat:'我的系统', account:'weixiang@evermind.ai',
    tools:['search_pages','create_page','update_page'], perms:['联网','写入外部内容'],
    why:'开启后 Raven 可以直接建页面，建议先确认目标库。' },
  { id:'pdf', name:'PDF 阅读', reach:'local', state:'add', glyph:'▥', ver:'0.1.4', src:'raven-pdf',
    one:'读长 PDF 并按页引用', cat:'文档',
    tools:['read_pdf'], perms:['读取文件'] },
  { id:'slack', name:'Slack', reach:'auth', state:'add', glyph:'◍', ver:'1.2.2', src:'slack.com/mcp',
    one:'读频道消息、发通知', cat:'我的系统',
    tools:['list_channels','post_message'], perms:['联网','以你的身份发言'],
    why:'这是「让它读写 Slack」。想在 Slack 里跟它说话，去「入口」。' },
  { id:'figma', name:'Figma', reach:'auth', state:'add', glyph:'◑', ver:'0.9.1', src:'figma.com/mcp',
    one:'读设计稿的图层与切图', cat:'我的系统',
    tools:['get_file','export_node'], perms:['联网'] },
  { id:'jira', name:'Jira', reach:'auth', state:'add', glyph:'◈', ver:'3.0.1', src:'atlassian.com/mcp',
    one:'建单、改状态、查冲刺', cat:'我的系统',
    tools:['create_issue','transition_issue','search_jql'], perms:['联网','以你的身份发言'] }
];

/* ══ module 4 data: scheduled work ═══════════════════════════════════
   A scheduled job is not a setting -- it is work that runs while you are
   away, and each run PRODUCES A SESSION. That is why it needs its own
   place: a list of jobs is useless without "what did it actually do".  */
const FREQ = [
  { id:'hour', label:'gui.freq.hour', at:false },
  { id:'day',  label:'gui.freq.day',  at:true },
  { id:'week', label:'gui.freq.week', at:true },
  /* The server's third kind. Without it a one-shot job had no frequency the
     editor could express, so reopening one and saving anything at all -- a
     rename -- rewrote it as a daily 08:00 recurring job under the same id, and
     cleared the flag that made it delete itself after firing. */
  { id:'once', label:'gui.freq.once', at:true },
  { id:'cron', label:'gui.freq.cron', at:false }
];

const CRONS = [
  { id:'j1', name:'昨日错误日志汇总', on:true,
    what:'读 ~/logs 下昨天的日志，按错误类型分组，超过 10 次的单独列出，写成一段简报。',
    freq:'day', at:'08:00', when:'每天 08:00', next:'明天 08:00', deliver:'feishu',
    runs:[
      { at:'今天 08:00', ok:true, ms:42000, note:'3 类错误 · 支付回调占 68%', sid:'r1' },
      { at:'昨天 08:00', ok:true, ms:38000, note:'2 类错误', sid:'r2' }
    ] },
  { id:'j2', name:'依赖安全告警', on:false,
    what:'跑一遍依赖扫描，只报 high 及以上，附上可直接执行的升级命令。',
    freq:'week', at:'周一 09:30', when:'每周一 09:30', next:'已暂停', deliver:'app',
    runs:[{ at:'上周一 09:30', ok:true, ms:96000, note:'2 个 high · 已给升级命令', sid:'r3' }] },
  { id:'j3', name:'竞品动态', on:true,
    what:'抓 Clay / 11x / Unify 的官网和博客，只报和上次相比的变化。',
    freq:'day', at:'19:00', when:'每天 19:00', next:'今天 19:00', deliver:'email',
    runs:[{ at:'昨天 19:00', ok:false, ms:8000, note:'网页搜索未配置，只抓到 1 家', sid:'r4' }] },
  { id:'j4', name:'周报草稿', on:true,
    what:'汇总本周的 commit 和已关闭的 issue，写成周报初稿。',
    freq:'week', at:'周五 17:00', when:'每周五 17:00', next:'周五 17:00', deliver:'app', runs:[] }
];

const DELIVER = { app:'gui.deliver.app', feishu:'gui.deliver.feishu', email:'gui.deliver.email' };
const cronFailing = () => CRONS.filter((j) => j.on && j.runs[0] && !j.runs[0].ok).length;

/* ══ module 2b data: channels — where you talk to it ═════════════════ */
/* Brand names stay as they are; the two generic ones (email, and the vendors
   whose English name differs) come from the catalogue. */
const CHANNELS = [
  { id:'feishu',   key:'gui.chan.feishu',   on:true,  who:'EverMind' },
  { id:'wecom',    key:'gui.chan.wecom',    on:false },
  { id:'weixin',   key:'gui.chan.weixin',   on:false },
  { id:'slack',    name:'Slack',    on:false },
  { id:'dingtalk', key:'gui.chan.dingtalk', on:false },
  { id:'qq',       name:'QQ',       on:false },
  { id:'telegram', name:'Telegram', on:false },
  { id:'discord',  name:'Discord',  on:false },
  { id:'whatsapp', name:'WhatsApp', on:false },
  { id:'email',    key:'gui.chan.email',    on:true,  who:'weixiang@evermind.ai' },
  { id:'matrix',   name:'Matrix',   on:false },
  { id:'mochat',   key:'gui.chan.mochat',   on:false }
];
// One accessor so a renderer never has to know which of the two it is.
const chanName = (c) => (c.key ? T(c.key) : c.name);

// Skills and plugins share a lifecycle, so most code treats them as one list;
// tools never do (they cannot be installed or removed, only switched off).
const INSTALLABLE = () => SKILLS.concat(PLUGINS);
const cap = (id) => INSTALLABLE().find((c) => c.id === id);
const capOn = (id) => { const c = cap(id); return !!c && c.state === 'on'; };
const tool = (id) => TOOLS.find((t) => t.id === id);
const needsAttn = (c) => c.state === 'need' || c.state === 'fail' || !!c.update;
/* Skills never block (they are method, not access), so the rail badge that
   says "something needs you" belongs to the plugins module alone. */
const attnCount = () => DS.plugins.rows().filter(needsAttn).length;

const STATE_TXT = {
  on:   { t: 'gui.state.on',   cls: '' },
  off:  { t: 'gui.state.off',  cls: '' },
  need: { t: 'gui.state.need', cls: 'warn' },
  fail: { t: 'gui.state.fail', cls: 'bad' },
  add:  { t: 'gui.state.add',  cls: '' }
};
const stateText = (st) => T((STATE_TXT[st] || STATE_TXT.off).t);

/* ══ scripted runs ════════════════════════════════════════════════ */
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

const GTM_DELIVERY_FILES = [
  { path:'research/gtm-compare.md', name:'gtm-compare.md', title:'GTM agent 赛道对比', size:1824, media_type:'text/markdown' },
  { path:'research/pricing.csv', name:'pricing.csv', title:'产品定价明细', size:936, media_type:'text/csv' },
  { path:'research/source-notes.pdf', name:'source-notes.pdf', title:'官网摘录', size:88420, media_type:'application/pdf' },
  { path:'research/market-map.xlsx', name:'market-map.xlsx', title:'市场分层', size:24118, media_type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
  { path:'research/brief.docx', name:'brief.docx', title:'研究摘要', size:16820, media_type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document' },
];

const FIX_DELIVERY_FILES = [
  { path:'internal/db/pool.go', name:'pool.go', title:'连接池修复',
    description:'后台任务使用独立连接池，登录请求增加 2 秒超时。', size:3412, media_type:'text/x-go' },
];

const GTM_FILE_EVENTS = [
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

const DAG_GTM = {
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

const RUNS = {
  gtm: {
    key: 'gtm', title: 'GTM agent 市场调研',
    ask: '调研一下市场上做 GTM agent 的产品',
    use: { calls: 4, in: 14226, out: 3180, cost: 0.021, wall: 41000 },
    /* 网页搜索未配置：搜索失败，降级为直接抓官网 */
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
    /* 网页搜索已配置：搜得到，覆盖面更宽，结论可交叉验证 */
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
const eventsFor = (run) => (run.key === 'gtm' && capOn('websearch') && run.evOk) ? run.evOk : run.ev;

/* ══ module 1 data: sessions ══════════════════════════════════════ */
/* Live mode persists a pin through this hook (session.pin); the demo has no
   server, so it stays null and pins live in page memory only. */
const SESSION_FIXTURES = [
  { id:'a', title:'GTM agent 市场调研', last:'抓取了三家代表产品的官网，出了对比表', when:'11:24', run:'gtm', pin:false },
  { id:'b', title:'修复登录偶发超时',   last:'3 runs, 0 failures · 已改连接池隔离',   when:'09:02', run:'fix', pin:true },
  { id:'g', title:'重构支付回调',       last:'出错：找不到模块 stripe',              when:'08:41', run:null, status:'err' },
  { id:'c', title:'整理本周迭代进度',   last:'还没开始',                            when:'昨天', run:null },
  { id:'h', title:'扫一遍依赖安全告警', last:'运行中 · 已查 12 个包',               when:'昨天', run:null, status:'run' },
  { id:'d', title:'把 CSV 导入 Notion', last:'还没开始',                            when:'周三', run:null },
  { id:'f', title:'给 README 加安装说明', last:'已导出 Markdown',                   when:'上周四', run:null },
  // Sessions a schedule produced. They get their own group because they
  // arrive while you are away -- mixed into 今天 they read as things you did.
  // Title is the job; the time badge tells the runs apart, so repeating it
  // in the title would just eat the width the title needs.
  { id:'k1', title:'昨日错误日志汇总', last:'3 类错误 · 支付回调占 68%',
    when:'08:00', run:null, from:'cron', job:'j1' },
  { id:'k2', title:'竞品动态', last:'网页搜索未配置，只抓到 1 家',
    when:'昨天 19:00', run:null, from:'cron', job:'j3', status:'err' },
  { id:'k3', title:'昨日错误日志汇总', last:'2 类错误',
    when:'昨天 08:00', run:null, from:'cron', job:'j1' }
];
