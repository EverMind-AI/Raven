# Playbook 文件格式

> **实现状态（2026-08-20，`refactor/unified_agent_registry`）**：节点定义已与 dag 收成一份
> （`DagNodeSpec`，camelCase 与 snake_case 两种 wire 拼写都留）。相对本文的三处变化：
> 节点级 `confirm` **字段删除**（闸只在图级）；`skills` / `mcps` 改为三态
> （不写 = agent 自己的菜单 / `[]` = 一个都不给 / 列表 = 收窄），空列表不再被折成"全集"；
> `instance` **真的生效**了（内置 agent 恒 stateful），限制改为"同句柄的非链头节点不得写
> skills/mcps"。`triggers` 与顶层 `confirm` 的职责改造（J 组）**也已落地**：漏斗与 LLM 门控
> 删除，入口是 `load_playbook` 一个工具，`confirm` 落在 dag 的图级参数上。
> 另外本文之外的新增：`agent` / `promptTemplate` 可以留空，由调用方用 `fills` 补齐，
> 见 §5 末。

一个 playbook = 一个目录，目录里一个 `playbook.md`，分三区。

```
~/.raven/playbooks/
  competitor-scan/
    playbook.md
```

````markdown
---
name / description                     # 身份信封
---

正文：给人读的说明书，机器不解析，两种 mode 一致

```yaml playbook-spec
全部机器字段
```
````

**判别靠路径，不靠字段**：在 `playbooks/` 扫描根下就是 playbook。不复用 skill 的 `SKILL.md`，所以不需要 `metadata: '{"raven": {"playbook": true}}'` 这类标记——同一件事有两个判据，就会出现"放在 skills 下却声明是 playbook""放在 playbooks 下却漏了声明"两种矛盾，而这两条规则本不需要存在。

**分三区而不是全塞 frontmatter**，理由是阅读顺序：正文说明书通常十几行，机器字段带上 `promptTemplate` 动辄上百行，短的在前长的在后才读得顺。

块内命名 camelCase。

---

## 1. frontmatter

两个字段，不可再加。放这里是为了索引器只读文件头就能建列表，不必解析全文。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 | 全局唯一 id，= 目录名，`^[a-z0-9][a-z0-9-]*$` |
| `description` | string | 是 | 一句话意图，写"什么时候该用我"，≤200 字 |

---

## 2. 块 · 顶层字段

| 字段 | 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| `version` | integer | 默认 `1` | 格式版本，非内容版本 |
| `mode` | enum | 必填 | 图从哪来：`dag` 图写死在 `nodes` ｜ `prompt` 图由模型按 `prompts` 当场组装 |
| `confirm` | boolean | 默认 `true` | 派发前是否要用户确认。**它是 dag 的图级参数**，本文件写的值随节点一起注入进去并被锁死（模型不可改）；闸在派发口上，不在发现路径上——模型选了剧本不等于用户同意跑它 |
| `triggers` | object | 必填 | 见 §3 |
| `params` | map | 可选 | 运行时入参，见 §4 |
| `nodes` | list | dag 必填 ≥1 / prompt 禁止 | 见 §5 |
| `prompts` | string | prompt 必填 / dag 禁止 | 组图指导：告诉模型怎么拼出一张图——用哪些 agent、分几层、谁依赖谁 |

`mode` 与 `nodes` / `prompts` 双向校验，违反即加载失败。

**两种 mode 只在"图从哪来"这一步不同，拿到图之后完全同一条链路**：过校验（§8）→ 确认闸 → 后台异步执行 → 回执 run_id → 完成后回注。所以 `confirm`、`instance` 规则、占位符对两种 mode 一致生效，不设执行形态字段。

`prompt` 模式组出来的图必须符合 §5 的 `nodes[]` 结构，跑之前过同一套校验，组不出合法图就报错而不是硬跑。

---

## 3. triggers

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `keywords` | list[string] | 是，≥1 | 归一化后子串匹配：小写、全半角折叠、空白压缩；中文不分词。长词短语都放这里 |

`keywords` 决定**这一轮把哪个剧本列到模型面前**——它是检索线索，不是触发器。库大了走 top-K 召回（照抄技能检索的做法），所以关键词的作用是提高召回率，不是保证被使用。命中不等于会跑：
剧本连同它的描述与参数表进入 `load_playbook` 工具的可选清单，用不用由模型带着整个会话上下文决定，
和技能检索里关键词的角色一致。库小的时候全部列出（`playbooks.router.topK` 默认 5，库不超过它就全列），
keywords 只影响排序与召回。

**两种成本分开处理**：`name` 的 `enum` 是**全库**（一个名字几个 token，召回漏了用户仍能点名叫），
而描述 + 参数表 + 留空字段清单**只渲染 top-K**（这才是贵的那部分，随 K 固定、不随库增长）。

**两种 mode 走同一条发现路径。** 不因 `mode` 分流——否则用户眼里同样是 playbook，一种会被自动触发、一种只能靠检索找到，这是能被直接感知的行为差异。

---

## 4. params.&lt;键&gt;

声明每次运行时可变的入参。`promptTemplate` / `prompts` 里用 `${params.<键>}` 引用。

| 字段 | 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| `type` | enum | 默认 `string` | `string` / `integer` / `number` / `boolean` / `enum` / `path` |
| `required` | boolean | 默认 `false` | 为真且没有值时追问，追问对象是模型（它去问用户） |
| `default` | any | 可选 | 有默认值则永不追问 |
| `enum` | list | `type: enum` 时必填非空 | 取值表。给模型做选择题，比填空准 |
| `description` | string | 必填 | 参数说明，同时用作缺参追问的话术 |

三个消费方：**执行入口的参数表**（这一整块要渲染进 `load_playbook` 的工具描述，模型靠它知道该传什么键——不渲染出去，模型只能猜键名，而猜错的键会被静默丢弃）、缺参追问、编译期替换 `${params.x}`。

`type: path` 编译期过 `check_confined`，不许逃逸出会话工作目录。

没有可变入参的 playbook（如"每周拉一次 issue 分诊"）整块省略。

---

## 5. nodes[]

| 字段 | 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| `id` | string | 必填 | `^[A-Za-z0-9_-]+$`，图内唯一，会成为产物文件名 |
| `agent` | string | 必填 | agent 注册表里的 name。接入方式（cli / acp / 进程内）、密钥、能力元数据全住注册表，playbook 不重复声明 |
| `promptTemplate` | string | 必填 | 本步任务书，占位符见 §6 |
| `dependsOn` | list[string] | 默认 `[]` | 依赖的节点 id，同时是引用白名单 |
| `skills` | list[string] | 可选 | 注入这个节点的 skills（只能从本机已有目录里挑，是收窄不是装载） |
| `mcps` | list[string] | 可选 | 注入这个节点的 mcp |
| `instance` | string | 可选 | 会话句柄，同句柄的节点共享一个 agent 会话 |
| `inputs` | map | 可选 | 每键为字面量字符串、`{file: 路径}` 或 `{node: id}`——**只有这三种，没有 run 限定符**（钉住某一次运行用 `{{ ref:@runs/<run_id>/… }}` 的文件形式，见 §6）。价值是**结构化**而非独有能力：`{{ inputs.k }}` 引用的 key 必须先声明（default-deny，内联的 `{{ ref: }}` 校验不了这一层），且看节点定义就知道它读什么，不必通读 prompt |

**这张表就是 dag 工具的节点 schema，不是 playbook 另定的一套。** per-node 配置是 dag 本身的能力，
playbook 加载后节点直接注入 dag 工具，所以模型直接调 dag 时也能写这些字段。

"同一份"指字段集与语义，有两处形态差异不算另定一套：模型面的 JSON Schema 用 snake_case（`prompt_template` / `depends_on`），这张表是写在文件里的 camelCase；以及派发时 `id` 会被加上 `<playbook 名>-<随机 6 位>-` 前缀，`dependsOn` 与 `{{ id.output }}` 同步改写——因为 dag 侧的 id 是**会话内唯一**，不改写的话同一个 playbook 在一个会话里跑第二次会被唯一性拒掉。前缀只活在运行时，不进文件。

`instance` 只管一件事：**要不要延续上下文**。`promptTemplate` 就是注入的那段 prompt，
不另设字段，延续上下文时照样能注入新的一段。

`skills` / `mcps` 当前只在会话启动那次生效（实现限制），所以只允许写在 `instance` 链的
**头节点**上——见校验规则 9。

配置挂节点不挂角色，因为同一个 agent 可以在一张图里跑多步、每步任务不同——
那只是两个任务、两份上下文，用了同一个 agent、同一份调用方式：

```yaml
nodes:
  - {id: a1, agent: research-raven, dependsOn: [],   skills: [市场调研]}
  - {id: b,  agent: code-raven,     dependsOn: [a1]}
  - {id: a2, agent: research-raven, dependsOn: [b],  skills: [代码审计], mcps: [github]}
```

**加载只有一个工具 `load_playbook(name, params, fills?)`，`mode` 决定加载之后怎么走**（模型不需要分辨 mode，清单里两种混在一起列）。两条执行面的保证强度不同：

| mode | 加载之后 | 谁按下派发键 | 能锁住什么 |
|---|---|---|---|
| `dag` | 引擎填参、注入节点，直接进 dag 链路；缺必填 `params` 或有留空字段就先回来问模型一轮 | **引擎** | **节点与闸全锁**。模型能传的只有 `params` 与 `fills`，没有语法表达"改一个已写字段"——结构性保证，不靠校验拦 |
| `prompt` | 返回填好 `${params.x}` 的组图指导 | **模型**（它据此自己写 `nodes` 调 `run_subagent_dag`） | 换来**灵活**：同一份指导按当次情况组出不同的图，这就是它存在的理由。代价是顶层 `confirm` 与步骤没有附着点（没有原图可比），要靠指导文字写清 |

两种 mode 是一对取舍，不是"一个完整一个残缺"：要**确定性**（跑一百次同一张图、闸和步骤都锁住）写 `dag`；要**灵活**（流程本身看情况定）写 `prompt`。把 `prompt` 模式的灵活当缺陷去补，等于把它变成一个更差的 `dag` 模式。

### 5.1 留空与 `fills`（已实现）

`agent` 与 `promptTemplate` 可以留空，意思是"这一处我不定，由调用方按上下文写"。只有这两个算**缺口**——
不写 `skills` 的意思是"用这个 agent 自己的菜单"，那是个完整答案，把它当缺口会让库里每张写得好的剧本
都先弹一个问题。

| | 行为 |
|---|---|
| 缺口的报法 | 缺的 `params` 与留空的节点字段**一起报**，且**零派发**。分两轮报等于让调用方花两次往返学一件事 |
| 补法 | `fills = {"<作者写的 node id>": {"promptTemplate": "..."}}`。键是**文件里的 id**，不是加了运行前缀的那个——前缀是之后加的，调用方从没见过它 |
| 硬规矩 | **`fills` 指向一个已写字段就整体拒**。没有这条，`fills` 就是个通用字段编辑器：改任意 prompt、把节点指到别的 agent、把 skills 拿掉，git 里的文件就不再描述真正跑了什么 |
| `skills: []` 不算留空 | 它是**写明了**的"一个都不给"。当成留空，调用方就能悄悄放宽这一步能碰的东西 |
| 回路上限 | 同一(会话, 剧本)最多报 2 轮缺口，超了返回终止语。"补不齐→再问→还是补不齐"是调用方能在一轮里空转掉的循环 |
| CLI | 没有模型可补，所以 `raven playbook run` 加了 `--fill NODE.FIELD=VALUE`；不给就报"这张剧本要在运行时补值"，不静默跑一张缺字段的图 |

`params` 与留空字段是两种不同的变化点：`params` 是**值**（一处声明、多处引用，带类型与必填校验），留空是**整个字段没写**（模型按上下文自由填）。

**字段不全是常态，三层各管一段。** 生产一份 playbook 时不必写满所有字段：

| 谁提供 | 内容 | 缺了怎么办 |
|---|---|---|
| 本文件 | `nodes[]` 的编排与每步配置 | 字段集是 dag 节点的子集，但**必填性更松**：`promptTemplate` 可以留空交模型补。其余五个 optional，**"不写"不等于"空"**，见下表 |
| 模型（读上下文） | `params` 的值 + 留空字段的 `fills` | `required: true` 且无 `default` 的必须有值；缺了由 `load_playbook` 结构化返回"还需要这些"、零派发，模型补齐后再调。**模型只能传 `params` 与 `fills`**——它没有语法表达"改一个已写字段" |
| 调用方 | `background` 等运行级参数 | dag 独有，本文件从不声明 |

| 字段 | 不写等于什么 |
|---|---|
| `skills` | **完整菜单**（不收窄），不是"无技能"。`skills` 是菜单过滤器不是装载器：写了是缩小候选集，不写是给全集，**两种情况下这一步实际用哪个技能都由跑这一步的 agent 现场按上下文决定** |
| `instance` | 运行时自动铸一个句柄、独立上下文，并在运行摘要里回报，后续可续用 |
| `mcps` | 不挂额外 mcp |
| `dependsOn` | 起点节点，与其他起点并发 |
| `inputs` | 无声明输入（`promptTemplate` 里仍可用 `{{ ref: }}` 直接读文件） |

所以"这一步挂什么技能留给模型按上下文挑"不需要特殊写法——省略 `skills` 就是它。反过来，**"这一步必须用某个技能"目前表达不了**：`skills` 只过滤菜单、不强制使用，只能在 `promptTemplate` 里用文字要求。

`mode: prompt` 是这套分层的极端情形：文件里一个节点都没有，全部节点字段由执行时那次 compose 调用产出，再过与 `mode: dag` 相同的校验（规则 12）。

**闸只有一级**，就是顶层 `confirm`：管"这个 playbook 要不要跑"。不设节点级闸——批准一张图本身是完整语义，审的时候看到的是全图。

实现上它就是 dag 的图级 `confirm`：本文件写的值随 nodes 一起注入，闸在 dag 工具的派发口上
（校验之后、计费之前——用户按掉的图不该花预算）。这个参数是**删漏斗的前置条件**：在它存在之前，
`confirm` 的唯一执行点在漏斗里，删掉漏斗等于让每张剧本都变成"模型一调就跑"。

既然选剧本这件事由模型决定，这一级闸就是**用户**唯一的介入点，所以配套要求是必需项而非可选项：确认对话框要列出哪些步骤有对外副作用（发布、提单、发信），让"点头"是知情的。判据可从注册表推导——该节点 agent 的 mcp / tools 里是否含写操作。

---

## 6. 占位符

| 语法 | 时刻 | 写在哪 | 含义 |
|---|---|---|---|
| `${params.x}` | 编译期 | `promptTemplate` / `prompts` | 参数值 |
| `{{ dep.output }}` | 运行期 | `promptTemplate` | 依赖节点的输出全文 |
| `{{ dep.output_path }}` | 运行期 | `promptTemplate` | 依赖节点输出的文件路径 |
| `{{ inputs.k }}` | 运行期 | `promptTemplate` | 节点 `inputs` 的值（file 形态取内容） |
| `{{ inputs.k.path }}` | 运行期 | `promptTemplate` | 节点 `inputs` 的文件路径（仅 file 形态） |
| `{{ ref:路径 }}` | 运行期 | `promptTemplate` | 工作区文件内容 |
| `{{ ref_path:路径 }}` | 运行期 | `promptTemplate` | 工作区文件路径 |

编译期只替换 `${…}`，`{{…}}` 原样透传给运行期。

两种 mode 都用这七个：`prompt` 模式先把 `${params.x}` 替换进 `prompts` 交给模型，模型组出的图里照样带 `{{…}}`，由 runner 在运行期解析。

`output` 与 `output_path` 的选择：本地 agent 传路径（自己去读，大产物不占上下文），远端 API agent 只能传内容。注册表里 `readsLocalFiles` 为假的 agent 用 `output_path` 会被预检拦下。

---

## 7. 图语言

`dependsOn` 是全部的图语言，并行隐式——没有依赖关系就并行，不需要 parallel 语法。

| 写法 | 含义 |
|---|---|
| `dependsOn: []` 或省略 | 图的起点，立即开跑 |
| `dependsOn: [a]` | 等 a 完成 |
| `dependsOn: [a, b]` | 等 a 和 b 都完成，汇合 |
| 两个节点写同一个上游 | 彼此无依赖，并行，分叉 |

```yaml
nodes:
  - {id: scan_market, dependsOn: []}
  - {id: scan_tech,   dependsOn: []}
  - {id: merge,       dependsOn: [scan_market, scan_tech]}
  - {id: selfcheck,   dependsOn: [merge]}
```

```
scan_market ─┐
             ├─→ merge ─→ selfcheck
scan_tech ───┘
```

`dependsOn` 同时是引用授权：`{{ x.output }}` 里的 `x` 必须列在本节点的 `dependsOn` 里，否则编译期报错（default-deny）。

`prompt` 模式组出来的图受同一套规则约束，不是逃逸口。

**表达不了的三件事**：环（条件回退，如"审校不通过退回重写"）、条件跳过（无 `when:`）、per-playbook 并发上限（由 runner 全局配置管）。

注意 `mode: prompt` **不能**绕开这三条。它是图的生成器，不是运行时编排器——图组装一次就固定，跑到一半不能根据中间结果改图。所以"连续两轮无新增发现就停"这类循环，两种 mode 都做不到；`prompts` 里只能写"怎么拼这张图"，不能写"跑起来之后怎么判断"。

---

## 8. 校验规则

加载时全跑，不过就进隔离区。

| # | 规则 |
|---|---|
| 1 | `mode: dag` → `nodes` 非空且无 `prompts`；`mode: prompt` → 有 `prompts` 且无 `nodes` |
| 2 | `frontmatter.name` = 目录名，且目录位于 `playbooks/` 扫描根下 |
| 3 | `nodes[].agent` 必须在注册表里，缺失则报"需要先注册 X"，不等到跑那一步才炸 |
| 4 | 图无环，且所有节点从起点可达 |
| 5 | `{{ x.output }}` 的 `x` 必须在本节点 `dependsOn` 内 |
| 6 | `${params.x}` 的 `x` 必须在 `params` 里声明 |
| 7 | `instance` 仅注册表标 `stateful` 的 agent 可用。**这条今天名存实亡**：只有生成器与 `raven playbook validate` 跑它，手写后直接运行会被静默丢弃（见统一注册表方案 §9） |
| 8 | 同 `instance` 的节点之间必须存在依赖链——共享会话不能并发，会互相踩上下文 |
| 9 | 同 `instance` 的**非链头**节点禁止出现 `skills` / `mcps` —— 会话启动后换不了，写了也只会静默失效。链头可静态判定（同 instance 成员必须构成依赖链，见规则 8）。`promptTemplate` 不受限，延续上下文时照样注入 |
| 10 | 同一个 `instance` 的成员必须是**同一个 agent**——不同 agent 共用句柄根本不共享会话（现有规则，沿用） |
| 11 | `{{ dep.output_path }}` 仅当注册表标该 agent `readsLocalFiles` 时可用 |
| 12 | `mode: prompt` 组出的图，执行前过 4-11 全部规则；不合法则重组，仍不合法则报错，不降级硬跑 |

---

## 9. 示例 · mode: dag

````markdown
---
name: competitor-scan
description: 对一家竞品分头做市场面与技术面调研，合并出一份带信源的报告。想快速了解某家竞品时匹配。
---

# 竞品快速扫描

市场面和技术面分两路并行查，合并成文后自查一轮。
每条结论必须挂信源，拿不准的标"待证实"。

```yaml playbook-spec
mode: dag
confirm: true

triggers:
  keywords: [竞品, 竞对, 对标, 竞争格局, 帮我看看这家公司]

params:
  target:
    type: string
    required: true
    description: 要扫描的竞品名称

nodes:
  - id: scan_market
    agent: research-raven
    dependsOn: []
    skills: [web-search, source-credibility-check]
    mcps: [exa]
    promptTemplate: |
      调研 ${params.target} 的市场面：定位、定价、客户结构、竞争格局。
      每个维度不超过 3 条要点，每条附来源。不碰技术细节。

  - id: scan_tech
    agent: research-raven
    dependsOn: []
    skills: [web-search, repo-analysis]
    mcps: [exa, github]
    promptTemplate: |
      调研 ${params.target} 的技术面：技术方案、开源生态、工程成熟度。
      不碰商业面。

  - id: merge
    agent: content-raven
    dependsOn: [scan_market, scan_tech]
    instance: w1
    promptTemplate: |
      合并两路发现成一篇报告，结论先行，每条结论挂信源。
      市场面：{{ scan_market.output }}
      技术面：{{ scan_tech.output }}

  - id: selfcheck
    agent: content-raven
    dependsOn: [merge]
    instance: w1
    promptTemplate: |
      按 checklist 自查：每条结论有信源、无未标注推测、无重复。
      改完输出定稿。
```
````

`scan_market` 与 `scan_tech` 都是起点，并行；`merge` 等两者完成；`merge` 与 `selfcheck` 共享 `instance: w1`，跑在同一个 content 会话里，所以自查时记得刚才写了什么。

---

## 10. 示例 · mode: prompt

````markdown
---
name: due-diligence
description: 对一家标的公司做尽调，深度按发现动态调整。投资、收购、合作前的背景核查时匹配。
---

# 尽调

调查范围随 focus 变，图的形状不固定，所以不写死 nodes，给组图规则。

```yaml playbook-spec
mode: prompt
confirm: false

triggers:
  keywords: [尽调, 尽职调查, 背调, 查一下这家公司]

params:
  target:
    type: string
    required: true
    description: 标的公司名称
  focus:
    type: enum
    enum: [tech, market, team, finance]
    default: market
    description: 侧重方向

prompts: |
  为 ${params.target} 组一张三层的尽调图，侧重 ${params.focus}。

  第一层，一个节点：
    agent: research-raven，skills: [web-search]
    任务是广度扫描，产出"已知 / 未知 / 存疑"三栏。

  第二层，按 focus 铺开，全部 dependsOn 第一层，彼此并行：
    - 恒定一个 research-raven 节点查团队背景与公开风险记录
    - focus 含 tech：加一个 code-raven 节点，mcps: [github]，扫开源仓库与技术博客
    - focus 含 market 或 finance：加一个 data-raven 节点，做可比公司与市场规模测算
    每个节点用 {{ 第一层节点id.output }} 拿到扫描结果，只深挖其中的"存疑"项。
    不要给这些节点设同一个 instance——各自独立上下文，防止早期错误结论在分支间放大。

  第三层，一个 content-raven 汇总节点，dependsOn 全部第二层节点。

  每个节点的 promptTemplate 都要写明：任何"该公司声称 X"必须标注信源与可信度，
  不得与已验证事实混排；查不到的写"未公开"，拿不准的标"待证实"。
```
````

`prompts` 写的是**怎么拼这张图**——分几层、每层用什么 agent 配什么 skills/mcps、谁 dependsOn 谁、节点间怎么传数据。不要写"跑起来之后怎么判断"，图组装完就固定了，运行期没有决策点。

---

## 11. 全字段索引

```
frontmatter   name · description

块顶层        version · mode · confirm · triggers · params · nodes · prompts
  triggers    keywords
  params.<键> type · required · default · enum · description
  nodes[]     id · agent · promptTemplate · dependsOn · skills · mcps ·
              instance · inputs

占位符        ${params.x}
              {{ dep.output }} · {{ dep.output_path }}
              {{ inputs.k }} · {{ inputs.k.path }}
              {{ ref:路径 }} · {{ ref_path:路径 }}（@runs/<run_id>/… 钉住某次运行）
```
