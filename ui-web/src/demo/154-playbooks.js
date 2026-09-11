/* ══ module 3b2: playbooks ════════════════════════════════════════
   The renderer is the playbooks island (ui-web/src/features/playbooks/): the
   library wall and one playbook's graph both draw off DS.playbooks. What
   lives here is the shell face -- the two verbs the rail and Escape call --
   and the fixture library, so the page is explorable with no engine behind
   it.

   The fixtures are wire-shaped (snake_case, exactly what playbooks.list and
   playbooks.get answer), because the island is the same code in both modes
   and a fixture in the island's own shape would hide a mapping bug until
   live. */

function openPb() { RavenIslands.playbooks.open(); }
function closePb() { RavenIslands.playbooks.close(); }
function drawPb() {
  /* A language flip re-renders #pbBody with the new catalogue. */
  RavenIslands.playbooks.redraw();
}

const PB_FIXTURE = [
  {
    name: 'release-notes',
    description: '看一个版本都改了什么，出一份分层的中文简报和一张长图',
    task_summary: '核对完整改动清单，逐个 PR 读实际改动，写成分层简报并出图',
    mode: 'dag',
    version: 1,
    confirm: true,
    origin: 'user',
    disabled: false,
    keywords: ['最新版', '发版', '发版通知'],
    params: {
      version: { type: 'string', required: false, default: '', description: '看哪个版本，留空取最新的' },
      lang: { type: 'enum', required: false, default: '中文', enum: ['中文', '英文'], description: '简报语言' },
    },
    prompts: '',
    path: '~/.raven/playbooks/release-notes/playbook.md',
    nodes: [
      { id: 'fetch', subagent: 'Raven', node_summary: '取版本区间内的完整 commit 与 PR 清单',
        prompt_template: '取 ${params.version} 的完整改动：tag 之间的 commit 与 PR 列表，以 commit 为准。',
        depends_on: [], skills: ['gh-cli'], mcps: null, instance: '', inputs: {} },
      { id: 'read_prs', subagent: 'Raven-Code', node_summary: '逐个 PR 读 diff，写清它改了什么行为',
        prompt_template: '逐个打开 {{ fetch.output }} 里的 PR，读 diff，写清实际改了什么行为。',
        depends_on: ['fetch'], skills: null, mcps: ['github'], instance: '', inputs: {} },
      { id: 'carry', subagent: 'Raven', node_summary: '核对上一版遗留的已知问题这版修没修',
        prompt_template: '翻上一版的已知问题清单，标出这版修没修。',
        depends_on: ['fetch'], skills: null, mcps: null, instance: '', inputs: {} },
      { id: 'write', subagent: 'Raven', node_summary: '按「不读会付出什么代价」分三层写简报',
        prompt_template: '按「不读会付出什么代价」分三层：必须知道 / 值得知道 / 可以跳过。\n改动：{{ read_prs.output }}\n遗留：{{ carry.output }}',
        depends_on: ['read_prs', 'carry'], skills: ['release-notes-voice'], mcps: null, instance: 'w1', inputs: {} },
      { id: 'poster', subagent: 'Raven-PPT', node_summary: '把简报排成一张竖版长图',
        prompt_template: '把简报排成一张竖版长图，标题用版本号。\n正文：{{ write.output_path }}',
        depends_on: ['write'], skills: null, mcps: null, instance: '', inputs: {} },
    ],
  },
  {
    name: 'issue-triage',
    description: '把仓库里新开的 issue 去重、分类、排出 P0-P3，P0 落成工单',
    task_summary: '拉本周新 issue，去重与分类并行，排完序把 P0 落成工单',
    mode: 'dag',
    version: 1,
    confirm: true,
    origin: 'user',
    disabled: false,
    keywords: ['整理 issue', 'triage', '排优先级'],
    params: {
      repo: { type: 'string', required: true, description: '仓库，owner/name' },
      window: { type: 'enum', required: false, default: '7d', enum: ['7d', '14d', '30d'], description: '看多久之内新开的' },
    },
    prompts: '',
    path: '~/.raven/playbooks/issue-triage/playbook.md',
    nodes: [
      { id: 'pull', subagent: 'Raven', node_summary: '拉时间窗内新开的 issue，整理成一张表',
        prompt_template: '拉 ${params.repo} 最近 ${params.window} 新开的 issue：编号、标题、报告人、正文摘要。',
        depends_on: [], skills: ['gh-cli'], mcps: null, instance: '', inputs: {} },
      { id: 'dedupe', subagent: 'Raven', node_summary: '找重复项与已修项，给合并建议',
        prompt_template: '在 {{ pull.output }} 里找重复与已修，输出合并建议，不要直接下关闭结论。',
        depends_on: ['pull'], skills: null, mcps: null, instance: '', inputs: {} },
      { id: 'classify', subagent: 'Raven-Code', node_summary: '逐条判类型与影响面，标可复现性',
        prompt_template: '逐条判定类型（缺陷 / 需求 / 提问）与影响面，标出可复现性：{{ pull.output }}',
        depends_on: ['pull'], skills: null, mcps: null, instance: '', inputs: {} },
      { id: 'rank', subagent: 'Raven', node_summary: '合并两路判定，按统一标准排出 P0-P3',
        prompt_template: '合并两路判定排出 P0-P3，每条写清判据。\n去重：{{ dedupe.output }}\n分类：{{ classify.output }}',
        depends_on: ['dedupe', 'classify'], skills: null, mcps: null, instance: 't1', inputs: {} },
      { id: 'file', subagent: 'Raven', node_summary: '把 P0 落成工单，先查重再挂本月迭代',
        prompt_template: '把刚排出的 P0 落成工单：先按标题查重，已存在的只补链接，不新建。',
        depends_on: ['rank'], skills: null, mcps: ['jira'], instance: 't1', inputs: {} },
    ],
  },
  {
    name: 'competitor-scan',
    description: '把一个竞品的市场面和技术面分两路查，合并成带出处的报告',
    task_summary: '并行调研市场面与技术面，合并成带出处的报告，再自查一轮',
    mode: 'dag',
    version: 1,
    confirm: true,
    origin: 'builtin',
    disabled: false,
    keywords: ['竞品', '对标', '看看这家公司'],
    params: { target: { type: 'string', required: true, description: '要看哪家公司' } },
    prompts: '',
    path: 'builtin: competitor-scan/playbook.md',
    nodes: [
      { id: 'market', subagent: 'Raven-Research', node_summary: '查市场面：定位、定价、客群、竞争格局',
        prompt_template: '调研 ${params.target} 的市场面：定位、定价、客群结构、竞争格局。每个维度最多 3 条，每条带出处。',
        depends_on: [], skills: ['web-research'], mcps: null, instance: '', inputs: {} },
      { id: 'tech', subagent: 'Raven-Code', node_summary: '查技术面：技术路线、开源生态、工程成熟度',
        prompt_template: '调研 ${params.target} 的技术面：技术路线、开源生态、工程成熟度。不要碰商业面。',
        depends_on: [], skills: null, mcps: ['github'], instance: '', inputs: {} },
      { id: 'merge', subagent: 'Raven', node_summary: '两路合并成结论先行的报告',
        prompt_template: '合并两路结论，结论先行，每条带出处。\n市场：{{ market.output }}\n技术：{{ tech.output }}',
        depends_on: ['market', 'tech'], skills: null, mcps: null, instance: 'w1', inputs: {} },
      { id: 'check', subagent: 'Raven', node_summary: '按清单自查定稿：出处、无臆测、无重复',
        prompt_template: '按清单自查：每条结论有出处、没有未标注的推测、没有重复。改完输出终稿。',
        depends_on: ['merge'], skills: null, mcps: null, instance: 'w1', inputs: {} },
    ],
  },
  {
    name: 'paper-to-deck',
    description: '把一篇论文读成能直接讲的中文讲稿，配一份成稿 deck',
    task_summary: '通读论文，复核方法与同期工作，写成讲稿并排成 deck',
    mode: 'dag',
    version: 1,
    confirm: true,
    origin: 'user',
    disabled: false,
    keywords: ['论文', '讲稿', '组会'],
    params: { file: { type: 'path', required: true, description: '论文 PDF 的绝对路径' } },
    prompts: '',
    path: '~/.raven/playbooks/paper-to-deck/playbook.md',
    nodes: [
      { id: 'read', subagent: 'Raven', node_summary: '通读全文，抽出主张、方法、证据三线',
        prompt_template: '通读 ${params.file}，抽出主张、方法、证据三线。',
        depends_on: [], skills: null, mcps: null, instance: 'd1', inputs: {} },
      { id: 'terms', subagent: 'Raven', node_summary: '把术语统一成一份中文对照表',
        prompt_template: '把术语统一成中文对照表。', depends_on: ['read'], skills: null, mcps: null, instance: 'd1', inputs: {} },
      { id: 'method', subagent: 'Raven-Code', node_summary: '复核方法部分的可复现性',
        prompt_template: '复核方法部分：数据、超参、评测口径是否够复现。',
        depends_on: ['read'], skills: null, mcps: null, instance: '', inputs: {} },
      { id: 'weak', subagent: 'Raven-Research', node_summary: '查同期工作，标出它没比较的对手',
        prompt_template: '查同期工作，标出它没有比较的对手。',
        depends_on: ['read'], skills: ['web-research'], mcps: null, instance: '', inputs: {} },
      { id: 'outline', subagent: 'Raven', node_summary: '排出讲稿大纲，先讲结论',
        prompt_template: '排出讲稿大纲，先讲结论。\n术语：{{ terms.output }}\n方法：{{ method.output }}\n同期：{{ weak.output }}',
        depends_on: ['terms', 'method', 'weak'], skills: null, mcps: null, instance: 'd2', inputs: {} },
      { id: 'script', subagent: 'Raven', node_summary: '按大纲写成逐页讲稿',
        prompt_template: '按大纲写成逐页讲稿。', depends_on: ['outline'], skills: null, mcps: null, instance: 'd2', inputs: {} },
      { id: 'qa', subagent: 'Raven', node_summary: '预演五个可能被问的问题并写答法',
        prompt_template: '预演五个可能被问的问题并写答法。', depends_on: ['script'], skills: null, mcps: null, instance: 'd2', inputs: {} },
      { id: 'deck', subagent: 'Raven-PPT', node_summary: '把讲稿排成 12-16 页 deck',
        prompt_template: '把讲稿排成 12-16 页 deck：{{ script.output_path }}',
        depends_on: ['script'], skills: null, mcps: null, instance: '', inputs: {} },
      { id: 'review', subagent: 'Raven', node_summary: '逐页核对讲稿与页面是否对得上',
        prompt_template: '逐页核对讲稿与页面是否对得上。', depends_on: ['deck', 'qa'], skills: null, mcps: null, instance: '', inputs: {} },
      { id: 'final', subagent: 'Raven-PPT', node_summary: '按核对结果改完，出终版',
        prompt_template: '按核对结果改完，出终版。', depends_on: ['review'], skills: null, mcps: null, instance: '', inputs: {} },
    ],
  },
  {
    name: 'multi-market',
    description: '同一套口径扫多个地区的市场，再横向对比给进入顺序',
    task_summary: '定统一口径，六地并行扫描，横向对比后给出进入顺序建议',
    mode: 'dag',
    version: 1,
    confirm: false,
    origin: 'builtin',
    disabled: false,
    keywords: ['多地', '市场扫描'],
    params: { product: { type: 'string', required: true, description: '扫哪个产品或品类' } },
    prompts: '',
    path: 'builtin: multi-market/playbook.md',
    nodes: [
      { id: 'frame', subagent: 'Raven', node_summary: '先定一套六地通用的口径与字段',
        prompt_template: '为 ${params.product} 定一套六地通用的口径与字段。',
        depends_on: [], skills: null, mcps: null, instance: '', inputs: {} },
      ...['cn', 'us', 'jp', 'sea', 'eu', 'mena'].map((r) => ({
        id: r, subagent: 'Raven-Research', node_summary: '按统一口径扫 ' + r,
        prompt_template: '按 {{ frame.output }} 的口径扫 ' + r + '。',
        depends_on: ['frame'], skills: ['web-research'], mcps: null, instance: '', inputs: {},
      })),
      { id: 'compare', subagent: 'Raven', node_summary: '六地横向对比，给出进入顺序建议',
        prompt_template: '六地横向对比，给出进入顺序建议。',
        depends_on: ['cn', 'us', 'jp', 'sea', 'eu', 'mena'], skills: null, mcps: null, instance: '', inputs: {} },
    ],
  },
  {
    name: 'dep-bump',
    description: '锁文件动之前，先看清这批升级会牵到哪些调用点',
    task_summary: '逐个待升依赖查变更日志与本仓调用点，出一份核对表',
    mode: 'dag',
    version: 1,
    confirm: false,
    origin: 'user',
    disabled: true,
    keywords: ['依赖升级', 'bump'],
    params: { path: { type: 'path', required: true, description: '仓库在本机哪个目录' } },
    prompts: '',
    path: '~/.raven/playbooks/dep-bump/playbook.md',
    nodes: [
      { id: 'check', subagent: 'Raven-Code', node_summary: '逐个待升依赖查变更日志与本仓调用点',
        prompt_template: '在 ${params.path} 里逐个待升依赖查变更日志，列出本仓受影响的调用点。',
        depends_on: [], skills: null, mcps: null, instance: '', inputs: {} },
    ],
  },
  {
    name: 'deep-research',
    description: '问题大到一次搜不完、而且要查到哪一步得看查出什么来时用',
    task_summary: '宽扫产出已知/未知/可疑三栏，按方向铺开深挖，最后汇总',
    mode: 'prompt',
    version: 1,
    confirm: false,
    origin: 'builtin',
    disabled: false,
    keywords: ['深度调研', '尽调', '背景核查'],
    params: {
      question: { type: 'string', required: true, description: '要查清什么问题' },
      focus: { type: 'enum', required: false, default: '市场', enum: ['技术', '市场', '团队', '财务'], description: '哪个方向重一点' },
    },
    nodes: [],
    path: 'builtin: deep-research/playbook.md',
    prompts: '为 ${params.question} 装配一张三层调研图，侧重 ${params.focus}。\n\n第一层，一个节点：\n  subagent: Raven，skills: [web-research]\n  任务是宽扫，产出「已知 / 未知 / 可疑」三栏。\n\n第二层，按侧重铺开，都 dependsOn 第一层，彼此并行：\n  - 固定一个 Raven 节点，查团队背景与公开风险记录\n  - 侧重含技术：加一个 Raven-Code 节点，扫开源仓库与技术博客\n  - 侧重含市场或财务：加一个 Raven 节点做可比公司与市场规模估算\n  每个节点通过 {{ 第一层节点id.output }} 拿到宽扫结果，只深挖「可疑」项。\n  不要给这些节点相同的 instance —— 各自独立上下文，早期错判不会跨支放大。\n\n第三层，一个 Raven 汇总节点，dependsOn 第二层全部节点。\n\n每个节点的 promptTemplate 都必须写明：凡「公司自称 X」必须标注来源与可信度；\n查不到的写「未披露」，不确定的标「待核实」。',
  },
  {
    name: 'repo-audit',
    description: '接手一个陌生仓库、或者上线前要过一遍安全时用',
    task_summary: '审计仓库的依赖、权限与密钥泄露风险',
    mode: 'dag',
    version: 1,
    confirm: true,
    origin: 'user',
    disabled: false,
    keywords: ['审计', '密钥'],
    params: {},
    prompts: '',
    path: '~/.raven/playbooks/repo-audit/playbook.md',
    nodes: [],
    error: "nodes.2.subagent: 'sec-raven' is not in the agent registry",
  },
];

/* A row is the list's view of a playbook; a detail is the whole thing. The
   fixture keeps one object per playbook and answers both from it, so the two
   calls cannot drift apart in demo mode. */
/* The credentials tab against the fixtures: what is "set" lives in memory for
   the page's lifetime, and authorizing flips a server to authorized after a
   beat, so the tab's every state is reachable with no engine behind it. */
const PB_CREDS = { params: {}, oauth: {} };
DS.playbooks ??= {
  credentials: async (name) => {
    const p = PB_FIXTURE.find((x) => x.name === name);
    if (!p) throw new Error('no playbook named ' + name);
    const secret = Object.entries(p.params || {}).filter(([, v]) => v.type === 'secret');
    const servers = Object.entries(p.mcp_servers || {});
    return {
      params: secret.map(([k, v]) => ({ name: k, set: !!PB_CREDS.params[name + '/' + k], description: v.description || '' })),
      servers: servers.map(([k, v]) => ({
        name: k, auth: v.auth || 'none', enabled: v.enabled !== false,
        authorized: !!PB_CREDS.oauth[name + '/' + k], shadows_host: false,
      })),
    };
  },
  setSecret: async (name, param) => { PB_CREDS.params[name + '/' + param] = true; },
  clearSecret: async (name, param) => { delete PB_CREDS.params[name + '/' + param]; },
  authorize: async (name, server) => {
    setTimeout(() => { PB_CREDS.oauth[name + '/' + server] = true; }, 1500);
    return { server, state: 'auth_required', auth_url: 'https://example.invalid/authorize?demo=1', error: null };
  },
  clearOauth: async (name, server) => { delete PB_CREDS.oauth[name + '/' + server]; },
  list: async () => PB_FIXTURE.map((p) => ({
    name: p.name,
    description: p.description,
    task_summary: p.task_summary,
    mode: p.mode,
    confirm: p.confirm,
    origin: p.origin,
    disabled: p.disabled,
    nodes: (p.nodes || []).map((n) => ({ id: n.id, depends_on: n.depends_on })),
    error: p.error || '',
  })),
  get: async (name) => {
    const p = PB_FIXTURE.find((x) => x.name === name);
    if (!p) throw new Error('no playbook named ' + name);
    return {
      name: p.name,
      description: p.description,
      task_summary: p.task_summary,
      version: p.version,
      mode: p.mode,
      confirm: p.confirm,
      origin: p.origin,
      disabled: p.disabled,
      path: p.path,
      keywords: p.keywords,
      params: p.params,
      nodes: p.nodes || [],
      prompts: p.prompts || '',
    };
  },
};
