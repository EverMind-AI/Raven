# 统一 Agent 注册表与节点注入：完整方案

**状态**：设计稿，待评审。
**调研基线**：`origin/main` @ `6acfa441`（2026-08-19，二次核实）。首稿基线 `07bc3294` 之后的五个提交全是前端（react island 化），本方案涉及的每个 Python 文件与首稿逐字节相同，调研结论无需重做；只有两个前端文件动了（`ui/src/demo/110-subagents.js`、`ui/src/live/240-external-agents.js`），影响 F 组的排期，见 §0.5。
**取代**：`2026-08-14-unified-agent-registry-design.md`（范围只到"builtin 上表、不新增字段"，本稿扩展到节点注入与 playbook 接线）。
**关联**：[playbook-spec.md](playbook-spec.md)（playbook 文件格式，本稿的节点 schema 与它共用一份）。

---

## 0. 框架总览：这套东西分几层，一次派活怎么走

### 0.1 四层，各层回答一个问题

| 层 | 回答的问题 | 载体 | 本次改造 |
|---|---|---|---|
| **能力登记** | 有哪些 agent 可调、怎么调、各自能干什么 | `subagents.agents[]` + `AgentRegistry` | **三份分裂收成一张表**，内置 agent 上表 |
| **编排** | 这件事分几步、谁做哪步、步间怎么传数据、每步注入什么 | dag 的节点与依赖 | **节点 schema 三份收成一份**，补齐注入字段 |
| **复用与分发** | 怎么把一份编排存下来、被自动触发、给别人用 | playbook 文件 | 从"平行体系"退回"编排层的配置文件" |
| **执行** | 怎么真的把任务跑起来 | backend（4 种 kind）+ runner 调度 | **不动**：调度逻辑与 backend 接口不变 |

改造全部落在前三层，且方向是收敛——删重复定义、删绕路机制、把散落的能力归位。执行层零改动是这次能低风险落地的前提。

### 0.2 派活的三种粒度，加一个图的来源

```
主 raven 自己做                    不派活
      |
spawn(agent, task)                 一个 agent, 一个任务
      |
run_subagent_dag(nodes)            多 agent, 多任务, 带依赖与并行
      - - - - - - - - - - - - -    以上是派活的粒度阶梯
playbook                           不是第四档粒度, 而是"图从哪来"的另一个来源:
                                   预制的 dag + 触发词 + 入参声明 + 可分发
```

前三档是**粒度**阶梯，playbook 是**来源**——它最终仍然落在 `run_subagent_dag` 这一档上，
只是图不由模型现场写。它的入口 `load_playbook` 和 spawn / dag 一样是模型手上的一个工具，
不比模型早、也不比模型晚（见 §0.3）。

后三者共用**同一张 agent 表**；后两者共用**同一份节点 schema 与同一条执行链路**。
spawn 不是 dag 的特例（它有自己的一次性回注语义），但选 agent 的依据必须一致——
否则就是现在这样：同一个 agent 在两个入口有两种命运。

### 0.3 一件事从用户的话到执行完

**一条原则：检索层只负责把候选摆到模型面前，用不用一律由模型决定。** skill、playbook、spawn 还是 dag，
都是模型在同一轮里做的同一类判断。

```
用户消息
   |
=== pre-turn: 检索与准备, 不做决定 ========================================
   |
[a] 剧本检索        触发词决定"这轮把哪些剧本列给模型看"
[b] 模型路由        select_model_chain: 这句话该用哪个模型
[c] 上下文装配      context engine 的 SkillForgeRouter 选 skills 渲染进 system prompt
   |
=== 主 raven 的一轮: 唯一的决策点 =========================================
   |
[d] 模型看到: 消息 + 被注入的 skills + 工具表(spawn / run_subagent_dag / load_playbook)
   |
   +-- 自己做, 不派活
   +-- spawn(agent, task)
   +-- run_subagent_dag(nodes)      自己写节点
   +-- load_playbook(name, params, fills?)     用某张剧本。模型不需要知道 mode,
                                                  分叉在引擎的执行面:
                                                  - dag 模式: 填参注入 -> 缺口回问
                                                    -> confirm 闸 -> **引擎派发**
                                                  - prompt 模式: 返回组图指导,
                                                    模型自己写 nodes 调上面那个 dag
                        |
        节点注入(id 加前缀, 不建 backend)  v
                        ------------> dag 入口 <------------
                                        |
                              校验(图 + 查表的能力预检) -> 计费
                                        |
                              ready-set 调度: 依赖满足即跑,同会话串行,其余并发
                                        |
                              每节点: registry.backend(node.agent, 本节点窄化) -> run
                                        |
                                    回执 run_id -> 完成后回注
```

**这是已定的方向，不是现状。** `origin/main` 今天是另一套：剧本漏斗跑在模型之前，命中即**接管整轮**、
主 raven 根本不启动（`agent/loop/main.py:3348`）。那套设计有三个问题——做决定的门控只看当前这一句、
零会话历史，比被它绕过的主 raven 掌握的信息更少；便宜的事（skill 命中只是注入）有模型判断，
贵的事（剧本命中直接起一整个后台 dag）反而没有；而 `declines` 这个机制的唯一用途就是抵消
"漏斗问过、模型不知道"的副作用。改造方案与删除清单见 §5.3，**与本注册表方案正交、单独一期**。

关键性质：**playbook 不是第二条执行路径**。它只在"图从哪来"这一段不同（文件里写死 / 模型按指导现场组），
拿到图之后走的是模型直调 dag 那条完全相同的链路——同一套校验、同一个调度器、同一条计费与回注。
这条性质决定了 playbook 里的字段不可能超出 dag 的能力，也决定了 dag 补一个能力，playbook 自动获得它。

### 0.4 产品上换来什么

| 现在 | 改后 |
|---|---|
| 内置 agent 在名单上不存在，模型不知道"不填 agent 会派给谁" | 名单一份含全部 agent，`spawn(agent=)` 必填，每次派活显式选人 |
| 剧本引用的四个名字是本系统虚构的，与注册表零关系；引用外部 agent 直接被拒 | 剧本用注册表的真名引用，表上有就能用；表上新增 agent，所有剧本立即可引用 |
| 剧本拿到别人机器上，缺 agent 时的失败点不明确 | 加载时明确报"需要先注册 X"——名字是约定俗成的，这是剧本可分发的前提 |
| 剧本里 `mcps` / `instance` 写了不生效或直接被拒 | 字段与 dag 能力一一对应，声明即可兑现（`mcps` 除外，能力单独一期，落地前显式降级告知） |
| trace 里 playbook 的每一步显示为 `pb-<node>`，看不出用了哪个 agent | 每步归因到真实 agent 名，与 spawn / dag 的记录同一命名空间 |

### 0.5 边界：本方案不动策略层

本方案是**注册与接线**的改造。以下三类属于策略层——它们决定编排效果，由算法侧拥有，本方案一律不改：

| 策略层资产 | 载体 | 本方案的处置 |
|---|---|---|
| **调度与派发语义** | runner 的 ready-set 循环、`instance` 分组串行、semaphore 限流、计费时机、`_mint_missing_instances` 自动铸句柄（其设计稿 [2026-08-17-auto-instance-handle-design.md](2026-08-17-auto-instance-handle-design.md) 已在 main） | 零改动 |
| **节点间交接物设计** | `{{ node.output }}` / `output_path` / `inputs` / `ref` 占位符体系、`<id>.out.md` 产物文件、`_namespace_run` 的 id 与引用同步改写 | 零改动，见 §5.3 末 |
| **模型面的调用教学** | dag 工具描述、`_NODE_SCHEMA` 各字段的描述文案、编排指导 skill `local/subagent-dag-orchestration`（`GUIDE_SKILL_ID`） | 只增字段不改既有文案；**新字段的措辞与 skill 正文的更新需算法侧过** |

下面四项**触到了模型面**，都做，且都已确认；列在这里是为了在排期时把回归成本算进去，不是待确认项：

| 项 | 触到什么 | 状态 |
|---|---|---|
| D5 的 `subagent` → `agent` 改名 | 模型调 dag 时写的参数名 | 做。口径统一，与"节点可指名表上任何 agent"是同一件事的两面 |
| D5 新增 `skills` / `mcps` 两个节点字段 | 进 `_NODE_SCHEMA` 即进模型的提示面；教模型组图的 `local/subagent-dag-orchestration` skill（`GUIDE_SKILL_ID`）正文要同步补"每步能注入什么" | 做。**skill 正文的更新是 D 组的一个独立条目（D10），首稿漏了** |
| §5.2 新增校验 (c) 同 instance 非链头禁传 skills/mcps | 收窄了可表达的图 | 做。禁掉的写法今天写了也静默失效（`raven_loop.py:244`），零表达力损失 |
| C2/C4 `spawn(agent=)` 改必填 + 描述改写 | 模型选人的提示面 | 做。C4 单独回归，见 §10 |

**这四项合起来是本方案对模型面的全部改动**，共同点是只改"有哪些 agent、每步能声明什么"，不改"怎么组图、怎么传交接物"。回归的关注点也在这里：模型是否还能正确选人、正确组图，而不是产出是否变好。

---

## 1. 现状与问题

### 1.1 三个入口，三套可见集合，任何两个都不相同

| | 内置 raven agent | 外部 agent（cli / acp / openai） | 名字来源 |
|---|---|---|---|
| **spawn** | 能调，但不在 roster 上——`agent` 为空或不认识就 fallback（[manager.py:194](../../raven/agent/subagent/manager.py)） | 能调 | `cfg.name` |
| **dag** | **调不到**：`subagent` 必填且只认三方名（[tool.py:130](../../raven/agent/subagent_dag/tool.py)） | 能调 | `cfg.name` |
| **playbook** | 能调，用四个硬编码名字 `<base>-raven`（[types.py:41](../../raven/playbook/types.py)） | **完全看不见**，校验直接拒 | 本系统独有 |

用户装了 claude_code：spawn 能派、dag 能派、playbook 报"未注册"。内置 agent 反过来：spawn 能用但模型在列表里看不到它、dag 用不了、playbook 用另一套名字用。这个差异用户无法理解，也无法从任何界面看出来。

### 1.2 一个源，四份数据

```
subagents.thirdParty[]  (config, 唯一真实来源)
   |--> SubagentManager._backends        第 1 次物化, spawn 用
   |--> SubAgentDagTool._subagents       第 2 次物化, 同一份 config 再 build 一遍
   |--> playbook 私有 dag tool           构造时快照, 永不刷新
BUILTIN_AGENTS + role_pool.py            playbook 自己的第 4 份, 与上面无任何连接
```

热更新（`apply_third_party_subagents`，[main.py:2192](../../raven/agent/loop/main.py)）只刷前两份，且两个 setter 各自 except-warning 跳过单条，"manager 有 hermes、dag tool 没有"是可能状态。第三份永远是启动时的样子。

一个现成的抓手：`RAVEN_LOOP_AGENT = "raven"`（[manager.py:46](../../raven/agent/subagent/manager.py)）已经是内置 agent 的保留身份，direct chat 和 instance registry 都在用——**内置 agent 在一个子系统里已经有名字，只是从未上过 roster**。

### 1.3 节点概念被定义了三遍

`playbook/types.py` 的 `NodeSpec`、`subagent_dag/_graph.py` 的 `DagNodeSpec`、`tool.py` 的 `_NODE_SCHEMA`（给 LLM 的 JSON Schema）。**没有哪一份是超集**：playbook 有 `skills` / `mcps` / `confirm` 而 dag 没有，dag 有 `inputs` 而 playbook 没有；agent 字段在 playbook 叫 `agent`、在 dag 叫 `subagent`；wire 拼写 playbook 是 camelCase（`CamelBase` 的 `to_camel`）、dag 是 snake_case。三份互不一致，导致：

- playbook 写 `mcps` → 不生效，只有一条事后降级 note（dag 侧零实现）；
- playbook 写 `confirm`（节点级）→ 声明即校验失败（dag 没有节点级闸）；**本方案的处置是删字段**，见 §4.5；
- playbook 写 `skills` → 生效，但走绕路：配置烧进预造的 backend 实例，dag 只认 `name -> backend` 字典，于是每个节点被迫造一个合成名 `pb-<node_id>`——**身份丢失**（instance registry 的 key、trace 归因全是 `pb-*`）。

---

## 2. 目标架构

### 2.1 一张表，三个消费方

```
                 subagents.agents[]  (config)
                          |
                          v  apply(): 唯一写入口, 启动与热更新同路
                  +----------------+
                  | AgentRegistry  |  一份物化: name -> (row, backend)
                  +----------------+
                    ^            ^
        取 backend  |            |  取 backend + caps
        + caps      |            |
                 +-------+   +----------------+
                 | spawn |   | run_subagent_dag|
                 +-------+   +----------------+
                                     ^
                                     |  节点原样注入(同一入口, 同一校验)
                             +---------------+
                             |   playbook    |  只声明编排与节点配置
                             +---------------+  不持 backend, 不解析 agent
```

三条原则：

1. **一张表**声明 raven 可调用的所有 agent——内置与外部——及各自的接入方式与能力参数。spawn 和 dag 读它判断有哪些 agent、怎么调、能不能共享会话、能不能传路径。
2. **playbook 是 dag 的子集**。它只是一份编排配置说明：加载后模型若选择使用，把节点配置与图级配置注入 dag 工具、补齐留空字段与运行级参数（`background` 等）去调用。子集关系覆盖 `nodes[]` **和图级配置**——`confirm` 也是 dag 的字段（见下）；只有 `triggers` / `params` / `mode` / `prompts` 是 playbook 独有的（管怎么被发现、怎么取参、材料给多少）。
3. **纪律：字段集 playbook ⊆ dag，任何时刻成立**（节点级与图级都算）。满足它的办法是抬高超集，不是缩小子集——playbook 声明了 dag 没有的字段，暴露的是 dag 的能力缺口，因为那些字段本来就是 dag 的语义。

   `skills` / `mcps`（节点级）和 `confirm`（图级）是同一个错误的三个实例：都是 playbook 写了、dag 没有，于是要么静默失效、要么声明即被拒。**`confirm` 是最晚被发现的一个**——本稿前几版还把它当作"playbook 独有"，那正是把违反纪律的现状当成了设计。

### 2.2 backend 与 roster（两个术语）

**backend** = "这个 agent 怎么执行任务"的那段代码，接口只有 `async run(task, ...) -> str`。四种 kind 各一个实现（`RavenLoopBackend` 进程内 / `CliAgentBackend` 子进程读 transcript / `AcpAgentBackend` 协议握手 / `OpenAIApiBackend` HTTP）。进度、并发闸、配额、回注都是 manager 的事，不在 backend 里——接入方式的全部差异被关在 backend 内部，这正是消费方能只认名字的原因。

**roster** = 渲染进工具描述、给模型看的名单文本，形如 `claude_code [stateful, local-files, live-progress] (描述...)`。它是模型唯一的信息来源——不在 roster 上的 agent 对模型等于不存在。三个能力标签正负都显式渲染（"没有标签"和"表里没说"对模型分不清）。工具描述负责告知、`subagent` 参数的 `enum` 负责限制，两者必须来自同一个集合。

---

## 3. 注册表设计

住 `~/.raven/config.json` 的 `subagents.agents[]`（读时兼容旧键 `thirdParty`）。一行 = 一个可调用的 agent。

### 3.1 公共字段

| 字段 | 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| `name` | string | 必填 | 全表唯一，`^[a-z0-9][a-z0-9_-]*$`，**创建时定、之后不可改**。模型在 `spawn(subagent=)` 和节点 `subagent` 里写的就是它 |
| `kind` | enum | 必填 | `builtin` / `cli` / `acp` / `openai`——接入方式，消费方只用它挑工厂分支 |
| `description` | string | 必填 | 给模型看的能力说明，写职责与边界 |
| `enabled` | boolean | 默认 `true` | 是否出现在 roster。记录用户意图，不由探测推导。**内置行没法"删"**（不写等于用包内默认），关掉一条内置行的唯一手段就是它 |
| `timeout` | int \| null | 默认 `null` | 无自动上限 |
| `maxOutputChars` | int | 默认 `30000` | 输出截断 |

关于 `name` 的三条已定判断：

- **不用 id**：模型只能读工具描述文本，id 给它零信息还多一次对错机会；playbook 要进 git diff 被人 review；注册表是每台机器自己的配置，id 只能本机铸造——引用 id 的 playbook 拿到别人机器上必定解析失败。分发要的是约定俗成的名字。
- **不可改、不做别名**：rename 的真实需求是"区分同一预设的两个实例"（`claude_code-work` / `claude_code-personal`），创建时选好名字即满足。alias 要真能保护历史引用就必须参与唯一性校验，那么历史上改过的每个名字都永久占位、只增不减。
- **唯一性在加载期去重并告警，硬拒仍只在写入路径**：现在只有写入路径（`update_subagents.py:137`）查重，手改 config 塞两个同名行能正常加载、`_backends[name]` 被后者静默覆盖。**但修法不能是在 schema 层抛 ValidationError**——仓库对这件事有明确既定原则，写在 acp 那条同类校验的注释里（`schema.py:1269`）：load 期硬拒会让整个顶层 `Config` 校验失败，于是 raven 起不来，而"能改这个字段的界面"正好在起不来的 config 后面。所以：**load 期去重 + warning（确定性保留首条），写入路径保持硬拒**。

### 3.2 分 kind 字段

| kind | 独有字段 |
|---|---|
| `builtin` | `skills` / `tools` / `model` / `restrictToWorkspace` |
| `cli` | `preset` / `command` / `resumeCommand` / `idSource` / `sessionIdPattern` / `outputPattern` / `transcriptFormat` / `readsLocalFiles` / `cwd` / `env` |
| `acp` | `preset` / `command` / `readyTimeoutMs` / `cwd` / `env` |
| `openai` | `preset` / `baseUrl` / `model` / `apiKey` / `systemPrompt` / `stateful` / `temperature` / `maxTokens` |

`preset` 运行时不读，纯溯源（`name` 用户创建时自选，可以不等于预设默认名；没有它无法区分"认领了预设槽位的行"和"手写行"，预设会被重复配出第二份）。builtin 行不需要它：内置行的 name 是包内已知集合，比对 name 即知是覆盖还是新增。

acp 行不该出现 `resumeCommand` / `stateful` / `readsLocalFiles` 等 cli 通道的**声明**字段——acp 的同类事实来自握手**协商**，并存即双源。现有约束，保留，包括它的**两档强度**：load 期 warn-and-drop（`schema.py:1267` 的 `_warn_on_declared_cli_fields`，理由同上——起不来的 config 修不了），写入路径硬拒（`update_subagents.reject_unsupported_acp_fields`）。这不是遗漏而是设计，新增的 name 唯一性校验按同一档位办。

### 3.3 示例

```jsonc
{
  "subagents": {
    "agents": [
      // ---- 内置行: 包内默认种子, 不写也存在; 写了是字段级覆盖 ----
      {
        "name": "research-raven",
        "kind": "builtin",
        "description": "深度检索与事实核查: 多源搜索、信源可信度判断、检索结果冲突时的裁决、带引用的研究结论。无信源不下结论。",
        "enabled": true,
        "skills": ["local/web-search", "local/source-credibility-check"],
        "tools": null,
        "model": null,
        "restrictToWorkspace": true
      },
      { "name": "code-raven",    "kind": "builtin", "description": "仓库级代码工作: 读懂现有结构、定位改动落点、带单测实现。遵循仓库既有抽象。", "enabled": true },
      { "name": "data-raven",    "kind": "builtin", "description": "数据全链路: SQL 工作流、统计与因果判断、量化影响面。结论必须可被程序断言验证。", "enabled": true },
      { "name": "content-raven", "kind": "builtin", "description": "文本交付物: 写作、改写、去 AI 味、格式与语气约束下出定稿。结论先行。", "enabled": true },
      { "name": "raven",         "kind": "builtin", "description": "通用子 agent, 不带能力偏置。", "enabled": true },

      // ---- 外部行 ----
      {
        "name": "claude_code",
        "kind": "acp",
        "preset": "claude_code",
        "description": "Claude Code over ACP - 强通用编码与 agent 任务。用本机已登录的 Claude Code。",
        "enabled": true,
        "command": "npx -y @agentclientprotocol/claude-agent-acp@0.66.0",
        "readyTimeoutMs": 120000,
        "cwd": null, "env": {}, "timeout": null, "maxOutputChars": 30000
      },
      {
        "name": "openclaw",
        "kind": "cli",
        "preset": "openclaw",
        "description": "OpenClaw CLI - 本地子进程, 按 transcript 读回结果。",
        "enabled": false,
        "command": "openclaw run --prompt-file {prompt_file}",
        "resumeCommand": "openclaw resume {agent_id} --prompt-file {prompt_file}",
        "idSource": "derived",
        "sessionIdPattern": "session: ([0-9a-f-]+)",
        "transcriptFormat": "openclaw_json",
        "readsLocalFiles": true,
        "cwd": null, "env": {}, "timeout": null, "maxOutputChars": 30000
      },
      {
        "name": "mirothinker",
        "kind": "openai",
        "preset": "mirothinker",
        "description": "MiroThinker HTTP 端点 - 忽略 system prompt, 重放对它无意义。",
        "enabled": false,
        "baseUrl": "https://api.example.com/v1",
        "model": "mirothinker-v1",
        "apiKey": "",
        "stateful": false,
        "timeout": null, "maxOutputChars": 30000
      }
    ]
  }
}
```

四条能力 builtin 行 + 一条通用 `raven` 行是包内种子；`raven` 同时承接历史身份——direct chat 目录（`subagents/direct/raven/<handle>/`）和 instance registry 里 agent 为 `"raven"` 的记录靠它对上，零迁移。用户可加第六条 builtin 行：backend 还是同一个进程内 loop，差别只在 skills / tools 白名单。

### 3.4 能力视图（配置说"怎么接入"，消费方读"能干什么"）

| 能力 | builtin | cli | acp | openai |
|---|---|---|---|---|
| `stateful` | 恒 `true`（raven 自持 message list 并重放，`instance_state.py`） | 由 `resumeCommand` 推导 | 由握手快照 `sessionCapabilities.resume` | 读声明，默认 `true` |
| `readsLocalFiles` | 恒 `true` | 读声明 | 恒 `true`（本地子进程） | 恒 `false`（schema 强制剥离） |
| `liveProgress` | `true`（已实测：`raven_loop.py:287/:304` 上报 usage 与 tool call，经 `activity.collecting(live_key=)` 进实时索引） | `false`（无 activity 上报，退出后读 transcript） | `true`（另有 in-flight transcript） | `false` |
| `injectable.skills` | 支持 | 不支持 | 不支持 | 不支持 |
| `injectable.mcps` | 支持（**dag 侧待实现**，见 §4.2） | 不支持 | 不支持 | 不支持 |

`injectable` 让"外部 agent 挂 skills 无处注入"从口头约定变成可校验字段。

### 3.5 代码形状

```python
@dataclass(frozen=True)
class AgentCaps:
    stateful: bool
    reads_local_files: bool
    live_progress: bool

@dataclass(frozen=True)
class Injectable:
    skills: bool
    mcps: bool

@dataclass(frozen=True)
class AgentRow:
    name: str
    kind: Literal["builtin", "cli", "acp", "openai"]
    description: str
    enabled: bool
    caps: AgentCaps
    injectable: Injectable
    config: Any                      # kind 独有字段, 只有工厂读它

class AgentRegistry:
    def apply(self, configs: list) -> None: ...
    """唯一写入口。启动与热更新同路, 建一次 backend。"""
    def rows(self) -> list[AgentRow]: ...        # 全表含 disabled, 给运维面
    def enabled(self) -> list[AgentRow]: ...     # roster 与 dag 校验用
    def get(self, name: str) -> AgentRow | None: ...
    def backend(self, name: str, *, build: Any = None) -> SubagentBackend: ...
    """外部 kind 返回共享实例; builtin 带 build 时按 skills/tools 白名单现建。"""
```

`enabled` 的作用面只有 roster，三个时刻用三个视图：**生成 playbook** 用 `enabled()`（不该生成引用已关闭 agent 的 playbook）；**加载校验** 用 `rows()`（名字在表上就合法——playbook 是分发单元，不该被本机开关状态判成坏文件）；**执行前** 检查 `enabled` 并给可操作错误（"agent X 已关闭，去打开或改这一步"）。

---

## 4. 统一节点 schema

一份 model + 一份从它派生的 JSON Schema。dag 拥有它；playbook 的 `nodes[]` 就是它。

**"同一份"指字段集与语义，不指 wire 拼写**：playbook 文件是 camelCase（`promptTemplate` / `dependsOn`，`CamelBase` 的既有约定），dag 给模型的 JSON Schema 是 snake_case（`prompt_template` / `depends_on`）。两种拼写都保留——前者是人写在 git 里的文件格式，后者是模型面的既有契约，改任一边都是无收益的破坏。统一发生在 python model 这一层（pydantic alias），executor 手搭 dict 的那段随之消失。

同理，注入**不是零转换**：`_namespace_run` 的 id 改写必须保留，见 §5.3 末。

### 4.1 字段

| 字段 | 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| `id` | string | 必填 | 任务名，`^[A-Za-z0-9_-]+$`，**会话内唯一**（后续图可引用它的输出），成为产物文件名 |
| `subagent` | string | 必填 | 调哪个 agent——注册表的 name。**D5 曾把它改名 `agent`，已回退**，见 §14 |
| `promptTemplate` | string | 必填 | 这一步做什么，就是注入的那段 prompt。占位符见 §4.4 |
| `dependsOn` | list[string] | 默认 `[]` | 执行顺序，同时是 `{{ }}` 引用白名单（default-deny） |
| `skills` | list[string] | 可选 | 注入本节点会话的 skills。收窄过滤器而非装载：只能从本机已有 skill 目录里挑，且叠在 `requires.tools` 过滤之上 |
| `mcps` | list[string] | 可选 | 注入本节点会话的 mcp server |
| `instance` | string | 可选 | 会话句柄：同句柄节点按序共享一个 agent 会话，可跨 run 复用。**只管上下文延续** |
| `inputs` | map | 可选 | 每键为字面量 / `{file: path}` / `{node: id}`。价值是结构化：引用前必须声明（default-deny），看节点定义即知它读什么 |

同一个 agent 在一张图里跑多步 = 多个任务、多份上下文、同一个 agent、同一份调用方式：

```yaml
nodes:
  - {id: a1, subagent: research-raven, dependsOn: [],   skills: [市场调研]}
  - {id: b,  subagent: code-raven,     dependsOn: [a1]}
  - {id: a2, subagent: research-raven, dependsOn: [b],  skills: [代码审计], mcps: [github]}
```

**闸只有一级，在整张图这一层，而且它是 dag 自己的字段**（图级 `confirm`，D13；playbook 的顶层 `confirm` 注入它）。节点级 `confirm` 不做——它要求 runner 具备 pause/resume，而"批准一张图"本身是完整语义：审的时候看到的是全图，点头就是对包含那一步的整张图点头。

代价是它必须换来一个前提：**确认时要能看清副作用落在哪几步**。所以确认对话框展示的不只是 playbook 名字与参数，还要列出哪些节点会产生对外副作用（判据是节点 agent 的 mcp / tools 里有写操作），例如"第 4 步 publish 会调用 x-write 对外发布"。一级闸加上信息充分的确认，比 N 个逐步闸更符合"一次审批一张图"的模型，也不需要动调度器。

如果以后出现"图跑一半必须停下来等人"的真实需求（不是安全兜底，而是流程本身要人参与），那是另一个能力——运行中挂起，需要 runner 支持中断与续跑，不在本方案范围。

### 4.2 注入的现状与工作量（差别很大，分开排期）

| 注入物 | dag 侧现状 | 要做的事 |
|---|---|---|
| `skills` | 无字段，绕路实现：`RoleBuildSpec(skills_allow=)` → `build_role_backend` → `RavenLoopBackend(skills_allow=)` → `build_subagent_prompt` 只把白名单里的 skill 摘要写进 system prompt。配置烧进 backend 实例、dag 只认字典——**合成名就是这条绕路的必然产物** | 搬正成节点字段，runner 解析时窄化。功能已有，只是搬位置 |
| `mcps` | **零实现**：`raven_loop.py` / `manager.py` 一个 mcp 字都没有；executor 只加一条事后降级 note | 真要建"给 subagent 挂 mcp server"。**建议单独拆一期**——注册表统一不依赖它 |
| `promptTemplate` | 已有 | 不动。不另设 system-prompt 追加字段：从写 playbook 的人的视角"注入一段 prompt"只要一个字段，落在 system 还是 user 槽位是实现细节 |

### 4.3 `instance` 与注入的关系（本期按现状约束）

`instance` 只管延续上下文。理想语义下延续上下文同时换一套 skills 是合法意图，但现有实现做不到：

[raven_loop.py:244](../../raven/agent/subagent/backends/raven_loop.py)——resume 时整条沿用历史里的 system prompt（重建会追加第二条 system turn），所以后续节点的 `skills` 白名单**静默失效**；skill 菜单在首次那条 system prompt 里定死。`tools_allow` 每次重建 registry 都生效，只有 `skills_allow` 卡在首次。旧 rule 9（"同 instance 的 skills 必须一致"）就是给这个限制打的补丁。

本期不改 backend，把约束改得更准——**禁止传入**而不是"必须一致"（后者允许在第 2、3 个节点重复写同一套，读的人以为每个节点都在配，实际只有第一次生效）：

| 层 | 规则 |
|---|---|
| 静态（校验期） | 同 `instance` 的**非链头**节点禁止出现 `skills` / `mcps`。链头可静态判定：同 instance 成员必须构成依赖链，链头就是启动会话那次 |
| 运行时（派发前） | 链头若碰到**已有 history 的句柄**（`instance` 可跨 run 复用），其 `skills` / `mcps` 同样无效 → 降级告知。这层静态判不出来 |

两条既有规则一并保留，不在本次改动范围：同 `instance` 的成员必须是**同一个 agent**（`validate.py`：不同 agent 共用句柄根本不共享会话）；未写 `instance` 的节点由 `_mint_missing_instances` 在校验后、开跑前自动铸一个并在运行摘要里回报。自动铸的句柄天然是单节点，所以上面那条"非链头禁传"碰不到它们。

`promptTemplate` 是无条件 append 的 user message，**两层都不受限**——延续上下文时照样注入新的一段。

放宽是后续可选项：resume 时用新 `skills_allow` 重建并**替换**第 0 条（不是追加）。改完 `instance` 回归纯粹语义、上面两条约束可删。不在首期。

### 4.4 占位符

| 语法 | 时刻 | 含义 |
|---|---|---|
| `${params.x}` | 编译期（playbook 填参） | 参数值。dag 直调时不存在这一层 |
| `{{ dep.output }}` / `{{ dep.output_path }}` | 运行期 | 依赖节点的输出全文 / 文件路径 |
| `{{ inputs.k }}` / `{{ inputs.k.path }}` | 运行期 | 节点 `inputs` 的值 / 文件路径 |
| `{{ ref:路径 }}` / `{{ ref_path:路径 }}` | 运行期 | 工作区文件内容 / 路径；`@runs/<run_id>/<node>.out.md` 可钉住某次运行 |

`output` vs `output_path`：本地 agent 传路径（自己读，大产物不占上下文）；注册表标 `readsLocalFiles=false` 的 agent 用 `_path` 形式会被预检拦下，错误信息给出内容形式的改法。

---

## 5. 运行时流程

### 5.1 spawn

**这张图是改造后的形态。**每行标了 `现状` / `改造` / `新增`——现状行照抄今天的代码，改造行是本方案要改的落点，新增行今天不存在。

```
模型读 spawn 的 description
  roster = registry.enabled() 渲染:                                   改造 今天只渲染三方行
  research-raven [stateful, local-files, live-progress] (深度检索与事实核查...);
  claude_code [stateful, local-files, live-progress] (...); ...
        |
        v
spawn(task="...", agent="research-raven", instance="topic-scan")
  agent 必填 -- enum 就是全表, 没有"省略即默认"这一档                    改造 今天可省略, 省略即 fallback
        |
        v
[1] 工具本地校验: instance 给了 & !row.caps.stateful -> 当场拒          改造 caps 由 _third_party_meta 扫描改为查表
[2] manager.spawn: instance_key = (quota_key, "research-raven", handle) 改造 今天内置一路记成 "raven"
    -> _write_spawn_status pending -> create_task                     现状
[3] 后台跑, 不阻塞本轮                                                 现状
[4] _run_subagent_inner 里的一次派发:
      backend = registry.backend("research-raven")                    改造 今天是 _resolve_backend, 不认识就静默 fallback
      async with hold_handle(session_key, agent, handle):             新增 C6 同句柄排队
          state.load() -> backend.run(...) -> state.save()            现状(裸跑) -> 新增的锁把它包起来
[5] 完成 -> announce 回注, 带 handle 供续用                            现状
```

[4] 的锁是 C6：今天这段 `load -> run -> save` 在 `manager.py:690` 没有任何互斥，
只有 `manager.chat`（direct chat）和 `CliAgentBackend.run` 持 `hold_handle`。
参见 §5.3 末的时序与四站点表。

改动五处：roster 含内置；`agent` **改必填**（无兼容代价：模型每轮重读工具描述，历史记录不重放）；stateful 判定一律查表；`RAVEN_LOOP_AGENT` 常量与**八处** `agent or/== RAVEN_LOOP_AGENT` 特例分支删除（manager.py:60/66/366/428/453/539/570/571，另 536 的 docstring 要改），`"raven"` 这个名字留给通用 builtin 行承接历史记录。570/571 在 `instance_state()` 里，是四条派发路径共用的重放判定——首稿漏计了这一处，它恰好是内置 agent"恒 stateful"的落地点。
第五处是 C6：`_run_subagent_inner` 的 `load -> run -> save` 纳入 `hold_handle`。

### 5.2 dag

**同上，这张图是改造后的形态**，逐行标注 `现状` / `改造` / `新增`。

```
run_subagent_dag(nodes=[...], background=true, confirm=false)         改造 confirm 是新参数(D13)
   |
[1] 拒绝子 agent 内调用(只有主 agent 编排)                             现状
[2] 暂停闸(用户 pause 了 delegation -> 整图拒, 零派发)                 现状
[3] 结构校验 parse_dag_spec                                          现状
[4] 图校验 validate_and_order:                                       现状
      id 会话内唯一 / 依赖存在 / 无环 / 起点可达
      {{ }} 只能引用本节点 dependsOn 内的 id (default-deny)
      文件引用根不逃逸 workspace
[5] 能力预检(caps 一律来自 registry.get(node.agent).caps):            改造 今天来自 _capabilities 字典
      (a) 2+ 节点共享 instance 而 agent !stateful       -> 拒          现状
      (b) 给 !readsLocalFiles 的 agent 传路径占位符      -> 拒          现状
      (c) 同 instance 的非链头节点带 skills / mcps       -> 拒          新增 见 §4.3
      (d) 节点写了 skills/mcps 而 agent !injectable      -> 降级告知    新增 不阻断
[6] registry.get(node.agent) is None -> 拒                           改造 今天查 self._subagents 字典
[7] confirm 闸: confirm=true -> 派发前问一次; 非明确同意 -> 零派发       新增 D13
      排在计费之前: 用户按掉的图不该花预算
      无 ask 通道时照样派发(与 playbook 顶层 confirm 同一取舍, §8)
[8] 计费(拒绝的图零预算)                                              现状 位置从"校验后"变成"闸后"
[9] _mint_missing_instances: 未写 instance 的节点各铸一个句柄           现状 不动
[10] run_dag -- ready-set 循环:                                      现状
      每轮: ready = 依赖全终态的 pending 节点
            按 instance 分组: 同组串行, 组间并发, semaphore 限流
      每节点: 渲染 {{ }}
              backend = registry.backend(node.agent,                 改造 今天从 subagents 字典取, 合成名走 run_with_roles
                              build=<本节点 skills/mcps 窄化>)
              async with hold_handle(session_key, node.agent,         新增 C6 同句柄排队
                                     node.instance):
                  state.load() -> run -> state.save()                现状(裸跑) -> 新增的锁把它包起来
              -> 写 <id>.out.md -> 终态入 store                       现状
[11] background=true: 第[9]步后立即回执 run_id; 跑完 announce 回注      现状
```

调度机制（ready-set、instance 分组、`\x00node\x00` 分组 key、自动铸句柄）全部不动；
计费本身也不动，只是被 [7] 的新闸挤到后面一位。改造集中在四处：caps 来源、backend 解析方式、
两条新校验（c/d）、图级 confirm，加一处新增的锁（C6）。

**C6 的锁位置有一个要注意的点**：它取在 semaphore 之内（`_run_node` 已经 `async with semaphore` 起头）。
于是一个等锁的节点会占着一个并发槽等——不会死锁（持锁那个必然会跑完），但同句柄节点很多时会挤占槽位。
反过来把锁取在 semaphore 之外，等槽期间就一直占着句柄，代价更大。选前者，并在实现里留注释说明。

`run_dag` 内部还会再查一次 roster（`subagents.get(node.subagent) is None`）——[6] 是把同一判断提前到调用者本轮，让拼错名字得到的是当轮可修的拒绝而不是下一轮的通告。改造后两处都变成查 registry，仍是两处，不合并。

### 5.3 playbook：从"漏斗拦截"改为"检索 + 模型决定"

> **本节分两半。** 前半是 `origin/main` 的现状（L1 索引、L2 门控、多候选裁决、confirm 三态、
> declines，全在 `raven/playbook/runtime.py` 与 `matcher.py`）；后半是**已定的改造方向**
> （见 §0.3 的原则：检索层只摆候选，用不用由模型决定）。
> 这一改造与本注册表方案**正交、单独一期**：两种设计的派发口都是 `run_subagent_dag`，
> 注册表、节点注入、backend 解析完全一样，区别只在"谁按下派发那一下"。
> 「派发之后」那一小节是两者共用的下游，本方案在那里只改一句：节点不再经 executor 建 backend。


#### 现状 · 启动时：索引怎么建

`store.list_ids()` 逐个 load，坏文件只记 warning 跳过（一个坏文件不能沉掉整个库）。
两类不进 L1 索引但仍留在库里：**没有 `triggers.keywords` 的**（永不被动命中，显式 run 仍可）、
**被 config `playbooks.disabled` 禁用的**（禁用只静音被动漏斗）。
索引里的词条在建表时就 `normalize` 过，所以查询是纯 `in` 子串检查。
建完跑一次 `find_collisions`，撞词只 warning——**运行时交给门控裁决**。

#### 现状 · 每条消息：两级 + 三个出口

```
用户消息
   |
[L1] 子串索引扫描(纯代码,零成本)
   |    normalize 一次, 遍历词条, 首次命中顺序去重
   +-- 0 命中 ----------------------------------> 落回正常对话(绝大多数消息)
   |
   1..n 候选(id + description + params 表)
   |
[L2] 一次 LLM 调用: 判意图 + 抽参 + 多候选裁决, 三件事一次做完
   |    输出 GateVerdict: match / confidence / reason / params / missing / contenders
   |    actionable = match 非空 AND confidence == high
   |
   +-- 调用异常 / 无 tool call / 坏 JSON / 未知 id --> 落回正常对话
   |     失败契约对齐 knn_router: 任何模型侧失败都归到"无匹配"
   |
   +-- match=null 或 confidence=low ------------> 落回正常对话
   |     门控 prompt 明确"宁可放过不要误配": 误配启动一整个后台运行,放过只是继续对话
   |
   +-- match=null + contenders >= 2 -----------> 交用户选(见下)
   |
   +-- match + high ---------------------------> 顶层 confirm 三态(见下)
```

#### 现状 · 命中多个：门控先裁，裁不出才问人

门控 prompt 里两条规则处理这件事：能分辨就**选最佳那一个**；
太接近分不出就 `match=null` + 把并列候选放进 `contenders`。后者进 `_offer_contenders`：

| 环节 | 行为 |
|---|---|
| 前置条件 | 有 ask 通道，且 `contenders` 里至少 2 个**已知且未禁用**的；不满足则落回正常对话 |
| 问法 | "这个请求符合多个剧本，跑哪个？"，选项 = 候选列表 + "都不是" |
| 选了一个 | **直接派发，不再二次确认**——从列表里挑本身就是同意 |
| 参数 | 抽参不是为任何特定候选做的，所以**以空参运行**，由缺参追问接手引导 |
| 答"都不是"或超时 | 把这批候选**全部**记进本会话 declines，落回正常对话 |

#### 现状 · 顶层 confirm 的三态：没有 confirm 怎么办

`confirm` 默认 `true`，但有三条实际路径：

| 情形 | 行为 |
|---|---|
| `confirm: false` | 直接派发，不问 |
| `confirm: true` 但**无 ask 通道**（`_ask is None` 或无 conversation id） | **照样派发**，只记一条 info。这是 confirm 引入前的行为，此时门控的 high-confidence 判定是唯一防线 |
| `confirm: true` 且有通道 | 问用户；除明确同意外的任何答复（**含超时的空答**）都跳过运行，并记进 declines |

第二条是设计上的取舍：无通道时选择"跑"而不是"不跑"，因为环境（测试、没有问答通道的渠道）不该让功能整体失效。
代价是这些环境下只有门控把关——所以门控的 `confidence == high` 门槛不能松。

#### 现状 · declines：为什么现在需要它

字段与理由都是现有的——[runtime.py:86](../../raven/playbook/runtime.py) 的注释里已写明「without the memory, the model re-runs what the user just declined」。这里展开说清，因为它是「用户拒绝过」这件事唯一的跨轮载体。

**问题在于拒绝发生在模型看到消息之前。** 用户说"帮我看看 Acme 这家公司"，触发词命中「竞品调研」，
问他要不要跑，他说"不用，我就是问问"——这一轮不跑，消息落回正常对话。

落回那一轮，主 raven 正常处理这条消息，而它手上有 `run_playbook` 工具。
它看到的是"用户在问 Acme 这家公司"，工具列表里有个「竞品调研」正好合适——
**它不知道三十秒前用户刚说过不要**，因为那次问答发生在漏斗里、模型的上下文之外。
没有这个机制，用户看到的是：问他要不要跑，他说不要，然后它还是跑了。

机制是一张 `会话 id -> 本轮被拒的 playbook 名字集合` 的表：

| 环节 | 行为 |
|---|---|
| 写入（两处） | 顶层 confirm 被拒 → 加那一个；多候选答"都不是"或超时 → 加**整批候选**（"都不是"否定的是整个提议） |
| 读取（一处） | `run_playbook` 工具入口。撞上不执行，返回一句给模型的指令："用户刚被问过并拒绝了，尊重这个决定，除非他们再明确要求" |
| 清空 | 下一条用户消息进 `consider()` 时。**per-turn 不是 per-session**——拒绝是对这一轮的回答，不是长期禁令，用户下一句"算了你跑吧"必须立刻能跑 |
| 不拦什么 | CLI 的 `raven playbook run` 与其他显式入口——那些是用户自己的动作 |

字段名 `declined` 读着像"被拒绝的剧本清单"，实际语义是"本轮不得由模型自主重试的名单"。

#### 改造方向一（已定）：漏斗降为检索，决定权交给模型

**关键事实：注入层已经存在。** `run_playbook` 的工具描述本来就把每个已装剧本
（`id: description`）逐条列给模型看，`name` 参数还带 `enum` 约束（`agent/tools/run_playbook.py`）。
所以这次改造不是"造一个注入机制"，而是**删掉 pre-turn 拦截**，让已经在那儿的工具去干活。
主体是减法。

**删除**

| 删什么 | 现在的作用 | 为什么能删 |
|---|---|---|
| `loop/main.py:3348` 的 `consider()` 拦截块 | 命中即接管整轮，主 raven 不跑 | 这就是改造的全部内容 |
| `_declined` / `reset_declines` / `run_named` 里的拒绝检查 | 跨层记住"用户刚拒绝过" | 漏斗没了就不存在"模型看不到的拒绝"——用户说"不用"就是这一轮对话的一部分，模型自己看得见 |
| `_offer_contenders` 及 `contenders` 分支 | 候选太接近时交用户选 | 多候选变成"多个候选一起列给模型"，模型挑或者自己去问 |
| `gate()` / `_GATE_PROMPT` / `GateVerdict` 的判定与裁决职责 | 判意图 + 抽参 + 多候选裁决 | 三件事全回到模型，且**省掉每条被提名消息的一次 LLM 调用** |
| `Origin.USER` 门槛 | 只对用户消息跑漏斗 | 没有漏斗就没有这个门槛 |

**保留但换职责**

| 留什么 | 旧职责 | 新职责 |
|---|---|---|
| `triggers.keywords` + `TriggerIndex` | 命中即触发 | **决定这轮把哪些剧本列进工具描述**。库小就全列（今天就是全列）；库大了用它收窄，和 skill 检索里关键词的角色完全一致 |
| 顶层 `confirm` | 漏斗派发前问用户 | **移到工具路径**——见下面那条坑 |
| `playbooks.disabled` | 只静音被动漏斗，显式仍可跑 | 语义要重定：没有被动漏斗了。建议 `disabled` = 不进 listing（模型看不见就调不到），`listing()` 里那个 `[disabled]` 标记随之删除 |

**必须补的一块：`confirm` 现在没有执行点。** `run_named` 走的是"caller 已经决定了"的假设，
**根本不过 confirm**，直接进 `executor.execute`（`runtime.py:159` 的 docstring 写明了这个取舍）。
今天 confirm 只在 `consider()` 里生效，所以删掉漏斗之后 confirm 会静默失效、每个剧本都变成"模型一调就跑"。
必须把闸挪到 `run_named`。而且方向上更该挪：模型成为唯一决策者之后，副作用闸比原来更重要，不是更不重要。
§4.1 那条"确认卡要列出哪些步骤有对外副作用"在这个设计下从可选变成必需。

**诚实的代价**

- **确定性下降**：模型可能不调、可能调错剧本、可能漏填参数。兜底是 `name` 的 `enum`、执行前的整图校验、以及缺参追问。
- **触发词的控制力下降**：写剧本的人从"我列的词命中就一定跑"退到"我列的词让它更容易被看见"。
- 换来的是：做决定的一方掌握全部会话上下文（今天的门控只看当前一句、零历史），
  以及一条更简单的链路——少一次 LLM 调用、少一个跨层状态（declines）、少一个用户交互分支（contenders）。

#### 改造方向二（已定）：两个工具——一个加载剧本，一个跑 dag；分叉在执行面

```
load_playbook(name, params, fills?)          <- 加载, 一个工具
     |
     +-- dag 模式     引擎填参注入 -> 缺口回问 -> confirm 闸 -> **引擎派发**
     |
     +-- prompt 模式  返回组图指导给模型
                            |
run_subagent_dag(nodes, ...)  <- 动态编排, 兼作 prompt 模式的落点
```

| 工具 | 职责 |
|---|---|
| `load_playbook(name, params, fills?)` | 用某张剧本。**模型不需要知道 mode**——`mode` 是作者的实现细节，清单里两种混在一起列，分叉发生在引擎的执行面 |
| `run_subagent_dag(nodes, background, confirm?)` | 动态编排：模型自己写节点。同时是 prompt 模式的落点 |

**为什么 `mode` 不该泄露给模型。** 它描述的是"这张剧本的材料给到什么程度"，属于作者的写法，
而模型要做的判断只是"这件事该不该用这张剧本"。让它按 mode 挑不同的工具，等于把作者的实现细节
变成模型可能搞错的一件事。工具名叫 `load`（而不是 `run`）也是这个理由：模型的动作是"加载"，
这次加载落在派发还是落在一段指导，由剧本决定、不由模型决定。

**dag 模式的完整流程：**

```
模型: load_playbook(name="topic-briefing", params={"topic": "Acme 的定价策略"})
   |
[1] 引擎填参: ${params.x} 代入各节点; id 加运行前缀(_namespace_run)
   |
[2] 检查缺口: 必填 params 无值? 节点有留空字段?
   |    +-- 有缺口 --> 结构化返回"还需要这些"(点名节点与字段), **零派发**
   |    |               模型下一次调用带 params/fills 补齐 -> 回到 [1]
   |    +-- 无缺口 --> 往下
   |
[3] 顶层 confirm 闸(图级 confirm, 见 D13)
   |
[4] 引擎派发 -> 并入 §5.2 的 dag 链路: 同一套校验、同一个调度器、同一条计费与回注
```

**派发键在引擎手里，这是关键。** 模型的动作只有"选哪张剧本 + 补缺口"，
按下派发那一下不是它的一次工具调用——所以不存在"加载完了却不派发"，也不存在"拿到剧本内容后自己把活干了"。

**"写明的不许动"靠入参形状 + 一条校验，不需要 diff。** 模型能传的只有 `params` 与 `fills`——
再加上"`fills` 的目标字段必须确实留空"这条校验（下面详述），"把节点 3 的 promptTemplate 改成别的"就表达不出来。
比"让模型重发整份节点、再用 diff 逐字段比对"简单得多，也不需要 **`fromPlaybook` 溯源参数**：派发发生在引擎内部，来源自明。

**`fills` 的形状与那条必须有的校验。** 键是**作者写的 plain node id**（不是加了运行前缀的那个——
现有代码已经遵守"notes 说作者的 id、runner 拿加前缀的 id"这个分工），值是 `{字段名: 值}`：

```
fills = {"draft": {"promptTemplate": "按 Acme 的定价页写一段对比"}}
```

**必须校验目标字段确实留空**——`fills` 指向一个已写字段就整体拒。上面说"没有语法表达改一个已写字段"，
准确说是"有了这条校验之后表达不出来"：不设它，`fills` 本身就是改任意字段的后门。这条校验是
"写明的不许动"的**实际执行点**，别把它当成可选的健壮性检查。

**缺口回路要有重试上限**（建议 2 次）。否则"补不齐 → 再问 → 还是补不齐"没有边界，
模型可能在一轮里反复调同一个工具。超限就返回一句终止语，让它改用别的方式或问用户。

**token 成本也降到最低。** 模型只在有缺口时才看到相关节点。像仓库里的 `topic-briefing`
（三个节点全写满、只有两个 params），模型一次调用就结束，全程没见过那三段 promptTemplate。

**`params` 与 `fills` 是两种不同的变化点，都需要：**

| | `params` | `fills`（留空字段） |
|---|---|---|
| 粒度 | **值**——往文本里插一个字符串 | **整个字段**——如 `promptTemplate` 一句话没写 |
| 复用 | **一处声明、多处引用**（`topic-briefing` 的 `topic` 出现在三个节点里） | 一处一处填 |
| 约束 | 有 `type` / `required` / `enum` / `description`，可校验、可追问、可评审 | 无，模型自由写 |
| 作者的意思 | "这里会变，而且我知道它是什么" | "这里我没定，你按上下文办" |

用 `fills` 模拟 `params` 会很糟（三个 promptTemplate 全留空，让模型自己保证三处主题一致）；
反过来也不行——空白的 `promptTemplate` 不是一次值替换。

**必填性纪律要精确化：字段集是子集，必填性可以更松。**

| 维度 | 关系 |
|---|---|
| `nodes[]` 的**字段集** | playbook ⊆ dag，任何时刻成立 |
| `nodes[]` 的**必填性** | **playbook ≤ dag（更松）**。dag 的 `required` 是 `id` / `subagent` / `promptTemplate`；playbook 可以留空 `promptTemplate` 交模型补 |

差额由 `fills` 补齐，最终由 dag 的校验兜底。**实现时不要照抄 dag 的 `required` 到 playbook 的 model 上**——
那会让"留空"过不了文件校验。

**两种 mode 是一对取舍，不是"一个完整一个残缺"。**

| | dag 模式 | prompt 模式 |
|---|---|---|
| 图从哪来 | 文件里写死 | 模型按指导现场组 |
| 换来什么 | **确定性**：节点、依赖、闸都锁住，跑一百次是同一张图 | **灵活**：同一份指导可以按当次情况组出不同的图——这就是它存在的理由 |
| 代价 | 情况变了就得改文件 | 顶层 `confirm` 与步骤没有附着点（没有原图可比），要靠指导文字写清 |

选哪个是作者对这件事的判断：流程稳定、或有对外副作用要卡闸的，写 dag 模式；
流程本身要看情况定的，写 prompt 模式——它的产出本来就不该等于任何预设的图。
**把 prompt 模式的灵活当缺陷去补，等于把它变成一个更差的 dag 模式。**

**`mode: prompt` 的 compose 调用在会话路径退役，但不能整段删。**
现在 `executor._compose` 是一次独立 LLM 调用（`build_compose_prompt` + `emit_graph` 工具 + 一轮修复），
用一份缓存 roster、看不到会话历史。会话里主模型直接拿指导文字自己出图，上下文更全、修复走常规工具报错回路，
所以这条路在会话里退役。

**陷阱：CLI 依赖它。** `raven playbook run` 没有模型在场，`_compose` 就是它把 prompt 模式剧本变成图的唯一手段
（CLI 正是为此才给 executor 传 `compose_model`）。整段删掉等于让 CLI 失去运行 prompt 模式剧本的能力。
二选一：**保留 `_compose` 专供 CLI**（推荐——代码已有、已有测试，`set_roster` 随 E2 改成从注册表取），
或明确宣布 CLI 只支持 dag 模式。同理，**有留空字段的 dag 模式剧本在 CLI 上也没人能补**：
要么加 `--fill <node>.<field>=<value>`，要么明确报"这张剧本需要由 agent 运行"，不能静默跑一张缺字段的图。

#### 派发之后：并入 dag

```
executor.execute(spec, params)
   |-- 填参: ${params.x} 填进节点; 缺必填 -> 返回追问, 零派发
   |-- mode 分叉: dag 直接取 nodes / prompt 一次 emit_graph 组图(不合法重组)
   |     ^ 会话路径下这一支退役(J15): prompt 模式改由主模型出图。
   |       但 CLI 没有模型在场, 这支要留给它(J16)
   |-- _namespace_run: id 加 <playbook>-<随机6位>- 前缀,          (现状, 保留)
   |     dependsOn 与 {{ id.output }} 同步改写, ref: 形式原样透传
   |-- 节点交给 dag 入口 —— 不解析 agent、不建 backend、不造合成名   <- 本次改动
   |
   v  并入 §5.2 的 [2]->[9], 一模一样:
   同一套校验、同一个 ready-set 调度、同一条计费与回注
```

`_namespace_run` **必须保留**，它不是本次要清理的绕路：节点 id 在 dag 侧是**会话内唯一**（这正是"上一次运行的产物还能被引用"的实现基础），而 playbook 作者写的是 `scan` 这类稳定短名，原样派发会让同一个 playbook 在一个会话里跑第二次直接被 id 唯一性拒掉。前缀里用随机 tag 而非自增计数，是因为计数随进程重启、而会话的 id 登记表活得比进程长。这段与占位符改写属交接物设计，见 §0.5。

要清理的只有 backend 那条绕路：**playbook 从头到尾不碰 backend。** agent 名解析、backend 构造、图校验全在 dag 层，
对"模型直调"和"playbook 注入"两个来源一视同仁。`prompt` 模式组出的图过同一套校验，不是逃逸口。

**同句柄并发：已定为排队，复用仓库已有的 `hold_handle`。**

这条不是 playbook 特有的，也**不是本方案新开的问题**——`spawn` 或 `run_subagent_dag` 连着调两次、
用同一个 `instance` 句柄，一样会撞。撞的是句柄那份会话状态：它住在
`instance_state_path(session_dir, agent, handle)`（`instance_state.py:36`），**键里没有剧本名、也没有 run_id**，
而更新方式是**整份读出 → 追加 → 整份写回**：

```
时刻 1   运行 A 读出记录 = [1, 2]
时刻 2   运行 B 也读出记录 = [1, 2]          <- A 还没写回
时刻 3   A 追加自己那一轮, 写回 = [1, 2, A]
时刻 4   B 追加自己那一轮, 写回 = [1, 2, B]   <- A 那一轮被整段抹掉
```

后果：**A 说的话丢了**；且 B 若在 A 写回之后才读，还会**看到 A 的上下文**——两次不相干的运行串味。
两件事都不报错。

**修法已经在仓库里。** `hold_handle(session_key, agent, handle)`（`instances.py:341`）是一把进程级、
按句柄键、任务内可重入的锁，它的 docstring 就是为这个场景写的：*"keyed by the handle rather than held on a
backend object, because the DAG tool and the sub-agent manager build separate backends from the same config"*。
问题只是**没有用在该用的地方**：

| load 到 run 到 save 的站点 | 是否持锁 |
|---|---|
| `manager.chat`（direct chat 整轮） | **是** |
| `CliAgentBackend.run`（有状态且具名句柄） | **是**（所以 cli agent 今天是安全的） |
| `manager._run_subagent`（spawn） | **否** —— `instance_state.load()/save` 裸跑 |
| `subagent_dag/runner.py:~560`（dag 节点） | **否** —— 同样裸跑 |

所以：**`openai` 声明 stateful 的 agent 今天就会撞**（它的会话状态正是 raven 重放的那份文件）；
本方案让 builtin agent 也能被 dag / playbook 指名，等于把这个已有缺陷的可触发面放大。

**改法（已定）**：把上面两个"否"的站点也纳入 `hold_handle`，同一把锁、同一个键——
即**同句柄排队**，先到先跑，后到的等。锁本身可重入，所以不会与 cli backend 内层那次获取死锁。
排队保住了"先后跑接着继续"这个特性（跨 run 复用会话），只挡掉时间重叠。

改造后立刻恢复的能力：

| 能力 | 改造前 | 改造后 |
|---|---|---|
| playbook 用外部 agent | 校验拒：`agent 'claude_code' is not registered` | 表上有就能用 |
| `instance` 共享会话 | 生成器与 `playbook validate` 拒；手写直接跑则**静默丢弃**（executor 组 `tool_nodes` 时不带它） | 内置行恒 stateful，写了就生效（并需要 §5.3 末那道并发闸） |
| handle 跨 run 复用 | instance registry 的 key 是 `pb-*` | key 用 `node.agent` 真名，与 spawn / dag 同一命名空间 |
| trace 归因 | 派发名 `pb-<node>` 看不出用了哪个 agent | 节点自带 `subagent`，合成名机制整体删除 |

---

## 6. 改造清单（按组）

### A 表本体
- **A1** 键名 `thirdParty` → `agents`，读时兼容旧键。
- **A2** union 加 `BuiltinAgentConfig`（`name/description/enabled` + 可选 `model/skills/tools/restrictToWorkspace`）。
- **A3** `roles_default.yaml` 四条转为表的内置种子行，加通用 `raven` 行；用户可覆盖、可增行。
- **A4** `third_party_agent_meta` 加 builtin 分支（`stateful=True` / `reads_local_files=True` / `live_progress=True`）。
- **A5** `RoleBase.tags` 死字段（只被自己的 `_check` 校验，无其他消费方）并入表行 capability。
- **A6** name 唯一性：**load 期去重 + warning（确定性保留首条）**，写入路径（`update_subagents.py:137`）保持硬拒。**不要在 schema 层抛 ValidationError**——理由见 §3.1，仓库对 acp 同类字段已经按这个档位办了（`schema.py:1267`），照抄那个形状。

### B 接入层
- **B1** 工厂加 builtin 分支 → `RavenLoopBackend`。难点：它要 provider / model / agent_home / exec_config 等运行时依赖，签名加 runtime context（`manager.build_role_backend` 已做过这件事，收编进统一工厂）。
- **B2** `enabled_third_party` → `enabled_agents`。
- **B3** roster 渲染支持 builtin 行。
- **B4** `AgentRegistry` 单份物化，manager 与 dag tool 持引用；热更新只刷一次，两边不可能不一致。

### C spawn
- **C1** roster 返回全表。
- **C2** `agent` **改必填**，enum 全表。
- **C3** stateful 判定一律查表。
- **C4** 工具描述文案改"从表里选一个"。**LLM-facing 变更，需回归**。
- **C6** **同句柄排队**：把 `manager._run_subagent` 与 `subagent_dag/runner.py:~560` 两处的 `instance_state` load-run-save 纳入已有的 `hold_handle(session_key, agent, handle)`（`instances.py:341`，进程级、按句柄键、任务内可重入）。现在只有 direct chat 与 cli backend 持它，所以重放路（openai 现在、builtin 改造后）裸跑。**必须与本方案同期**：让 builtin 能被 dag 指名会放大这个既有缺陷的可触发面。
- **C5** 删 `RAVEN_LOOP_AGENT` 与**八处**特例分支（manager.py:60/66/366/428/453/539/570/571，另 536 docstring）；签名收掉 `| None`；`"raven"` 名字留给通用 builtin 行。570/571 在 `instance_state()` 里，是内置 agent "恒 stateful" 的实际落地点，改它等于改四条派发路径共用的重放判定——单独写测试。

### D dag（含节点 schema）
- **D1** 去掉第二份物化，引用 B4。**纯重构零行为变化，可先做**。
- **D2** 节点可指名表上任何 name（含内置）。
- **D3** capability 预检从表派生；对 map 外名字保持宽松默认（测试替身不该被拒）。
- **D4** **三份节点定义收成一份**：一份 model + 派生 JSON Schema，dag 拥有，playbook 复用。收的是字段集与语义，**两种 wire 拼写都保留**（playbook 文件 camelCase / 模型面 snake_case），见 §4 开头。
- **D5** 节点补 `skills` / `mcps` 两字段；`subagent` 改名 `agent`；`inputs` 保留（价值是 default-deny 声明与可审阅性，已有完整实现与测试，删是净成本）。**不加节点级 `confirm`**，见 §4.1 末。改名与新字段都触模型面，见 §0.5。
- **D6** 删 playbook 的 `nodes[].confirm` 字段。现状"字段存在的唯一用途是被拒绝"（validate.py:95）——处置是删字段而非实现它；同时把顶层确认对话框补上"哪些步骤有对外副作用"的展示。
- **D7** 删 `run_with_roles` 的 backend 注入（`roles` 字典），runner 按 `registry.backend(node.agent, build=...)` 解析。安全属性反而更强：能写进节点的只有表上已有的 name 加只能收窄的白名单，从"把入口藏起来"变成"结构上不可能"。
- **D8** 图校验收拢为 dag 一套（playbook 的 `validate.py` 与 dag 的 `_graph.py`/`_capabilities.py` 现在重复且已漂移）；新增 §5.2 的 (c)(d) 两条。
- ~~**D9**~~ resume 时刷新 system prompt——**移出首期**，本期按 §4.3 的禁传约束；放宽属后续可选项。
- **D13** dag 补**图级 `confirm`** 参数（不是节点级——节点级仍然不做，见 §4.1）。`confirm: true` 时派发前问一次；默认 `false` 以保持现状。与 D5 补 `skills`/`mcps` 是同一条子集纪律的三个实例：playbook 写了、dag 没有。**J 组依赖它。**
- **D10** 更新编排指导 skill `local/subagent-dag-orchestration`（`tool.py` 的 `GUIDE_SKILL_ID`）：节点字段名改了、多了两个可注入字段，而这份 skill 才是"节点接线规则"的真正载体——工具描述里放不下，所以描述只负责把模型指过去。**D5 落地必须带上它**，否则模型按 skill 的旧正文组图、按新 schema 校验，两边不一致。正文由算法侧定稿。

### E playbook（主要是减法）
- **E0** 删自己的 `NodeSpec` 与图校验，复用 D4。
- **E1** 删 `BUILTIN_AGENTS`；校验必须传入来自表的 `known_agents`；CLI 离线校验拿不到表时明确报"agent 名未校验"，不拿假清单判。
- **E2** `role_pool` 只留作 A3 种子。`base_of` 真死（只被 `__init__.py` 导出，零调用），直接删；`agent_roster` **有两个活调用点**（`agent/loop/main.py:1124`、`cli/playbook_commands.py:190`，都是给生成器喂 roster），是**改指向 registry**而不是删——两处都要跟着改。
- **E3** 删死参数 `load_role_pool(overrides)`（config 无 `playbooks.roles` 键，无生产者），随 A3 转为表 override。
- **E4/E5/E7** executor 不再建 backend、不再硬编码 `AgentCapabilities(stateful=False)`、合成名机制删除——三项随 D7 整段消失。
- **E8** 外部 agent 注入的校验随 D8 移到 dag 层，按 `injectable` 降级告知（能力缺口降级、安全闸失败即拒的既有原则）。
- **E9** 生成器 inventory 接真实 skill / mcp 清单。现状 `StaticInventory()` 无参 → 清单恒空 → `check_assets` 把每个 skill 判成 unknown。**独立 bug，可先修**。

### F 运维面
- **F1** `subagents.list` 加内置行，`group` 新增"内置"档（不能落进 uninstalled）。
- **F2** 前端连接页（`ui/src/live/240-external-agents.js` + demo 侧 `110-subagents.js`）加内置档：不可删、不可改 command、不可探测。**排在 react island 化之后**——这两个文件正是 `6acfa441` 一线改动的对象，抢在中间做会撞。
- **F3** 两条写入路径都要挡住"删除或改写内置行的接入方式"：`rpc/methods/subagents.py`（add/update/toggle/remove）与 `web_rpc/methods_config.py:147`。首稿只点了后者。
- **F4** probe 跳过内置行与 disabled 行（后者现在也在白花最多 10s/行）。
- **F5** `subagents.test` 对内置行的语义待定（跑一次 in-process spawn，或不提供）。
- **F6** `"raven"` 保留名归属通用 builtin 行（随 C5）。

### J playbook 改为"模型决定 + 单一加载入口"（单独一期，与 A-I 正交，依赖 D13）

**删（漏斗那套）**
- **J1** 删 `loop/main.py:3348` 的 `consider()` 拦截块；`consider` 退化为"这轮列哪些剧本"的检索，或整个并入 `listing()`。
- **J2** 删 `_declined` / `reset_declines` / `run_named` 的拒绝检查——跨层补丁，随漏斗一起消失。
- **J3** 删 `_offer_contenders` 与 `contenders` 分支：多候选变成多个候选一起列给模型。
- **J4** 删 L2 `gate()` / `_GATE_PROMPT` / `GateVerdict`：判定、抽参、裁决全回模型，每条被提名消息省一次 LLM 调用。

**建（单一加载入口）**
- **J5** `run_playbook` 改名 `load_playbook`，扩展为唯一加载入口：新增 `fills`，新增**缺口回路**（缺必填 `params` 或有留空节点字段 → 结构化返回"还需要这些"、点名节点与字段、零派发；模型补齐后再调）。**执行面按 mode 分叉**，`mode` 不进工具签名。**LLM-facing，需回归。**
- **J6** `fills` 的形状与校验：键为**作者写的 plain node id**（不是加了运行前缀的 id——现有代码就遵守"notes 说作者的 id、runner 拿加前缀的 id"），值为 `{字段名: 值}`。**必须校验目标字段确实留空**：`fills` 指向一个已写字段就整体拒。这是"写明的不许动"的实际执行点——不设这条校验，`fills` 就成了改任意字段的后门。
- **J7** 缺口回路要有**重试上限**（建议 2 次）：超过就返回一句给模型的终止语，避免"补不齐 → 再问 → 再补不齐"无界循环。
- **J8** `confirm` 落在 D13 的图级 `confirm` 上：dag 模式剧本的顶层 `confirm` 与 nodes 一起注入。**D13 是 J 组的前置依赖**（今天 confirm 只在被删的 `consider()` 里，`run_named` 明确跳过闸）。
- **J9** playbook 的 `NodeSpec` 必填性放松（`promptTemplate` 可留空）：字段集仍是 dag 的子集，必填性更松，差额由 `fills` 补齐、dag 校验兜底。实现时**不要照抄 dag 的 `required`**。
- ~~**J-x**~~ `fromPlaybook` 溯源 + diff 校验：**不需要**。派发在引擎内部完成，模型能传的只有 `params` / `fills`，配合 J6 的校验，"改一个已写字段"表达不出来——结构性保证优于校验拦截。

**改（字段职责与文案）**
- **J10** `triggers.keywords` 换职责：从"命中即触发"改为"决定这轮把哪些剧本列进 `load_playbook` 的描述"——它是检索线索，不是触发器。
- **J10b** **照抄 skill 侧的检索收窄**，不把整库丢进上下文。`SkillForgeRouter` 现成的形状：每轮 `select` 出 `top_k=5`，`over_fetch_factor=2` 先多取再收窄，配置住 `skillForge.router`（剧本只有本地一个来源，所以不需要它那套跨源带权 RRF 融合）。对应加 `playbooks.router.topK` 等配置。
- **J10c** **但两种成本要分开处理**，因为 skill 与剧本的失败代价不同：skill 没召回顶多少一份参考，**剧本没召回就是调不到**。所以 `name` 的 `enum` 用**全库**（只是名字，100 张约 500 token），而**描述 + 参数表 + 留空字段清单只渲染 top-K**（这才是贵的部分，每张约百 token）。这样召回失败时用户显式点名仍能调到，工具报错也能给出全库清单，而贵的部分随 K 固定、不随库增长。
- **J10d** 照抄机制、**不共用实例**：剧本不是 skill，混进同一个 router 会污染 skill 的召回结果。
- **J11** `playbooks.disabled` 语义重定为"不进 listing"；删 `listing()` 里的 `[disabled]` 标记。
- **J12** `listing()` / 工具描述带上**每个剧本的参数表**（名字、描述、`required`、`enum`）以及**有哪些留空字段**。J4 删掉门控就删掉了参数表的唯一读者（`matcher.py:139-141`），不补这条，模型只能猜键名，而猜错的键被 `_fill_params` 静默丢弃 → 追问 → 再猜。**与 J4 同一个 PR。**
- **J13** 重写 `_missing_params_reply`：收件人从用户改为模型（现在的话术是"回复里带上一个触发词"，那是无状态漏斗的产物）。
- **J14** 改 `load_playbook` 的描述文案：现在写着"当请求匹配某个剧本但**没被自动捕获**时用它（被动匹配器只认特定触发词）"——没有被动匹配器了，改成正面表述。**LLM-facing，需回归。**

**compose 与 CLI（这里有个陷阱）**
- **J15** 会话路径不再走 `executor._compose`：prompt 模式改由主模型拿指导文字自己出图，上下文更全、修复走常规工具报错回路。
- **J16** **但 `_compose` 不能整段删** —— CLI 的 `raven playbook run` 没有模型在场，`_compose` 是它把 prompt 模式剧本变成图的唯一手段（它就是为此才传 `compose_model`）。删了等于让 CLI 失去运行 prompt 模式剧本的能力。二选一：**保留 `_compose` 专供 CLI**（推荐，代码已有且有测试；`set_roster` 随 E2 改指向注册表），或明确宣布 CLI 只支持 dag 模式。
- **J17** CLI 遇到**有留空字段**的 dag 模式剧本同样无人可补：要么加 `--fill <node>.<field>=<value>`，要么明确报"这张剧本需要由 agent 运行"。**别让它静默跑一张缺字段的图。**

**文档与测试**
- **J18** `playbook-spec.md` 的 `triggers` / `confirm` / 字段完整性三节改写；`test_playbook_tool.py` 与漏斗相关用例整体重写；`integration/test_playbook_real_llm.py` 的门控用例全部作废。

### G/H/I 迁移、测试、文档
- **G1** 旧配置 `thirdParty` 键读时兼容、写回统一新键。改名的爆炸半径比首稿写的大：`schema.py` 的 `SubagentsConfig.third_party`、`update_subagents.py` 全套 helper、`rpc/methods/subagents.py`、`web_rpc/methods_config.py`（含 `SubagentsConfig(third_party=agents)` 这类构造）、`agent/loop/main.py:2192` 的 `apply_third_party_subagents`，以及 `enabled_third_party` / `third_party_agent_meta` / `add_third_party_subagent` 三个函数名（B2 只改了第一个）。
- **G2/G3** 归零——内置行沿用 `research-raven` 等现名，已有 playbook 与 instance 记录零迁移。
- **H** **10 个测试文件**直接引用会被删/改名的符号（`test_playbook_{executor,generator,tool}`、`test_subagent_{acp,manager,third_party}`、`test_subagent_dag_runner`、`test_update_subagents`、`test_web_rpc_config`、`integration/test_playbook_real_llm`）；把 `third_party` / `"subagent":` 这类更宽的口径算上共 **25 个**要过一遍。首稿写的 16 两头都不对。
- **I** 更新 `2026-08-11-external-agent-registry-design.md`（R1/R2 被扩展）、`2026-08-14-playbook-field-definition.md`（§5.1 中 instance 一行理由与代码不符）、`CONTEXT.md` 术语。**不动** `2026-08-17-auto-instance-handle-design.md`（自动铸句柄）与 `local/subagent-dag-orchestration` 之外的调度类设计稿——按 §0.5，那些是策略层资产。

---

## 7. 分期

```
P0  D1 (dag 去第二份物化)              纯重构, 独立交付, 立刻消掉热更新不一致窗口
 |
P1  A + B (表本体 + AgentRegistry)     spawn/dag 行为保持不变
 |
 +-- P2  C + D2/D3 (spawn/dag 读表)    C4 是 LLM-facing, 单独回归
 +-- P3  D4-D8 + D10 + E (节点统一 + playbook 接线)   不依赖 P2, 可并行
 |        D10(编排 skill 正文) 必须与 D5 同一个 PR
 +-- P4  F (运维面)                    F2 排在前端 react island 化之后
 |
P5  G/H/I 随各期分摊

单独一期(已定):  J 组 -- playbook 改"模型决定 + 单一加载入口"。与 A-I 正交:
                两种设计的派发口都是 run_subagent_dag, 注册表/节点注入/backend
                解析完全一样。主体是减法, 但有三个不可省的前置/配套:
                  D13 (dag 图级 confirm)  否则闸没有落点
                  J6  (fills 目标必须留空) 否则 fills 是改任意字段的后门
                  J12 (参数表进工具描述)   否则模型只能猜键名
                以及一个不能删错的地方: J16, CLI 仍需要 _compose。

单独一期(建议):  mcps 注入 -- "给 subagent 挂 mcp server"是从零建的新功能,
                注册表统一不依赖它; 拆出去后首期没有从零建的功能。
                在它落地前, dag 对 mcps 的行为 = injectable 判定 -> 降级告知。
```

## 8. 已定决策

| 决策 | 结论 |
|---|---|
| 内置行 name | 沿用 `research-raven` 等现名。后缀无害，为"干净"付迁移不值 |
| `spawn(agent)` | 必填，删默认分支与 `RAVEN_LOOP_AGENT` |
| `"raven"` 保留名 | 归通用 builtin 行 |
| name vs id | name；不可改；无别名机制 |
| builtin `liveProgress` | `true`（已按代码实测） |
| 合成名 / `run_with_roles` | 删除；节点自带 `subagent`，runner 查 registry |
| `instance` 语义 | 只管上下文延续；本期非链头节点禁传 `skills`/`mcps`（实现限制），`promptTemplate` 不受限 |
| 节点级 `confirm` | **不做**。闸只在整张图一级。附带一条**本稿提出、尚未拍板**的产品要求：确认卡要展示哪些步骤有对外副作用（判据是节点 agent 的 mcp / tools 含写操作），否则"一次审批一张图"缺信息 |
| name 唯一性的校验档位 | load 期去重加 warning，硬拒只在写入路径。**修正首稿**：schema 层硬拒会让 raven 起不来，见 §3.1 |
| wire 拼写 | playbook 文件 camelCase、模型面 snake_case，两种都留；统一只发生在 python model 层 |
| `_namespace_run` | 保留。playbook 节点 id 的前缀改写是"id 会话内唯一"的必要桥，不是要清理的绕路 |
| 无 ask 通道时的 confirm | **保留"照样派发"**。不是所有 IM 渠道都有问答能力，无通道不该让 playbook 整体失效；那些环境下门控的 `confidence == high` 是唯一防线，所以该门槛不得放松 |
| 多候选裁决 | 门控先裁（"能分辨就选最佳那一个"），分不出才交用户选；从列表里挑即同意，不再二次确认，以空参跑并由缺参追问接手 |
| `mcps` | 字段进 schema，能力单独一期；落地前降级告知 |
| 同句柄并发 | **排队**。复用已有的 `hold_handle`，把 spawn 与 dag runner 的 load-run-save 纳进去（C6）。先后跑仍然是"接着继续"，只挡时间重叠 |
| **谁决定用不用 playbook** | **模型**。检索层只把候选摆到模型面前，用不用由它决定——skill / playbook / spawn / dag 是同一类判断，不该一个走拦截、一个走注入。取代现状的 pre-turn 漏斗拦截，见 §5.3 与 J 组 |
| 随之删除 | `declines`（跨层补丁）、`contenders` 用户选择分支、L2 门控的判定与抽参职责 |
| **playbook 怎么进 dag** | **两个工具**：`load_playbook(name, params, fills?)` 与 `run_subagent_dag`。加载只有一个入口，**分叉在引擎的执行面**——dag 模式填参注入后由引擎派发（缺口先回问一轮），prompt 模式返回组图指导让模型自己组图。`mode` 不进工具签名，模型不需要分辨 |
| **写明的字段** | **不许动，且是结构性的**：模型能传的只有 `params` 与 `fills`，没有语法表达"改一个已写字段"。因此不需要溯源参数，也不需要 diff 校验 |
| 必填性纪律 | 字段集 playbook ⊆ dag；**必填性 playbook ≤ dag（更松）**，差额由模型补齐 |
| `mode` 的地位 | **保留，但只对引擎可见**：它决定加载后走派发还是走指导，是作者的写法而非模型的选择项。两种 mode 的保证强度不同（dag 锁节点与闸，prompt 把组图权委托给模型），所以有对外副作用的流程应写成 dag 模式 |
| `confirm` 归属 | **dag 的图级参数**（D13）。playbook 的顶层 `confirm` 与 nodes 一起注入，写明了就被 diff 锁死。修正本稿前几版把它当作 playbook 独有的说法——那是把违反子集纪律的现状当成了设计 |
| 剧本完整时模型看到什么 | **什么都不用看**。无缺口时一次 `load_playbook` 调用即派发，模型全程没见过节点内容 |

## 9. 顺带修掉的既有缺陷

1. **内置 agent 的 stateful 三处矛盾**：manager 与 spawn 说恒真（机制：`instance_state.py` 重放），executor 硬编码 `False`，已合入 main 的 field-definition 文档写"无 resume 机制"与代码不符。统一表后自动一致。
2. **`instance` 的设计意图两头都没实现**。`validate.py` 的模块 docstring 写的是"rule 7 在 v1 放宽：`instance` 在**执行时 strip 并加一条 note**，而不是在这里拒绝"。实际：(a) `validate_graph_nodes` 就在"这里"拒了，与自己的 docstring 直接矛盾；(b) executor 确实 strip 了（组 `tool_nodes` 时不带 `instance`）但**没有 note**（只有 `mcps` 有降级 note）。于是用户写了 `instance`，要么被生成器拒、要么静默失效。
3. **`validate_structure` 覆盖不到运行时**：`store.load` 只做 pydantic 校验，dag 模式的 `execute()` 不做图校验，所以手写的 playbook 里凡是"结构级"违规（`instance`、节点级 `confirm`、`${params.x}` 引用未声明的参数）在运行时都不会被发现。D8 把图校验收拢到 dag 一套之后自动覆盖，但**这条得显式验一遍**，别以为改完就自然好了。
4. **`skills: []` 从 playbook 表达不出来**：`raven_loop.py` 支持"整个隐藏菜单"这一档，但 executor 传的是 `node.skills or None`，空列表被折成 `None` = 全集，语义正好相反。修法是把 `or None` 换成显式的"字段缺失 → None / 空列表 → []"。
5. **三个空旋钮**：`RoleBase.tags`（无消费方）、`load_role_pool(overrides)`（无生产者）、`StaticInventory()`（清单恒空，E9）。
6. **`skills_allow` 与 `tools_allow` 的 resume 行为不一致**（§4.3），本期以禁传约束外化，后续可修。

## 10. 风险

| 风险 | 缓解 |
|---|---|
| C4 改工具描述改变模型选 agent 行为 | 单独回归；spawn 的 enum 硬约束兜底 |
| 旧配置 `thirdParty` 键 | 读兼容 + 写回迁移，一个版本内双读 |
| playbook 存量文件引用 `pb-*` 时代的行为 | 无：合成名从未出现在 playbook 文件里，只在运行时 |
| dag 节点 schema 加字段对 LLM 的提示膨胀 | 只加 `skills` / `mcps` 两个字段，description 精简到一行 |
| D5 改 schema 而漏改编排 skill | D10 与 D5 同一个 PR 落地，schema 与 skill 正文不允许分两次改 |
| F 组与前端 react island 化撞车 | F2 排在 island 化之后；两个文件（`240-external-agents.js` / `110-subagents.js`）是一线正在改的对象 |
| 同句柄并发（同一会话、同一 agent、同一 `instance`）会互相覆盖会话状态 | **既有缺陷**，不是本方案引入：`openai` 声明 stateful 的 agent 今天就会撞。已定改法 = 同句柄排队，把 `_run_subagent` 与 dag runner 的 load-run-save 纳入已有的 `hold_handle`（C6）。本方案让 builtin 也能被 dag 指名，所以列为**必须同期修** |
| J 组：`fills` 成为改任意字段的后门 | J6 的"目标字段必须确实留空"校验是硬要求，不是健壮性检查 |
| J 组：删 `_compose` 让 CLI 跑不了 prompt 模式剧本 | J16：保留 `_compose` 专供 CLI，或明确宣布 CLI 只支持 dag 模式 |
| J 组：缺口回路无界重试 | J7 的重试上限（建议 2 次）+ 终止语 |
| J 组：剧本库变大后工具描述膨胀 | 照抄 skill 侧的检索收窄（J10b-J10d）：描述只渲染 top-K，`enum` 保持全库以免召回失败变成能力缺失 |

---

## 11. 二次核实记录（相对首稿的修正）

基线从 `07bc3294` 重跑到 `6acfa441`，十处 `file:line` 引用全部复核通过。以下九处首稿有误或有漏，已就地改：

| # | 首稿 | 实际 | 落在 |
|---|---|---|---|
| 1 | 基线 `07bc3294` | 已漂到 `6acfa441`；中间五个提交全是前端，本方案涉及的 Python 文件逐字节未变 | 文档头 |
| 2 | "playbook 的 `NodeSpec` 字段最全" | 谁都不是超集：playbook 缺 `inputs`，dag 缺 `skills`/`mcps`/`confirm` | §1.3 |
| 3 | name 唯一性"上移到 schema 层" | **设计错误**。仓库既定原则是 load 期 warn、写路径 reject（`schema.py:1269` 写明理由：load 期硬拒 = raven 起不来 = 修它的界面也进不去） | §3.1 / A6 |
| 4 | acp 字段"禁止出现" | 是两档：load 期 warn-and-drop，写路径硬拒 | §3.2 |
| 5 | playbook 节点"原样交给 dag，零转换" | `_namespace_run` 会改写 id / `dependsOn` / `{{ }}`，必须保留；wire 拼写两侧也不同 | §4 开头、§5.3 末 |
| 6 | `RAVEN_LOOP_AGENT` 六处特例 | **八处**，漏的两处（570/571）在 `instance_state()` 里，正是内置"恒 stateful"的落地点 | §5.1 / C5 |
| 7 | dag 流程缺一步 | `_mint_missing_instances` 在计费后、开跑前给未写 instance 的节点铸句柄 | §5.2 / §4.3 |
| 8 | E2 "删 `agent_roster`" | 它有两个活调用点，是改指向而非删；`base_of` 才是真死 | E2 |
| 9 | "16 个测试文件" | 直接引用被删/改名符号的是 10 个，宽口径 25 个 | H |

另新增两项首稿完全漏掉的改动面：**D10**（编排指导 skill 正文必须随节点字段同步改）与 **§5.3 末的 `instance` 跨并发运行问题**（交算法侧定）。

---

## 12. 实现记录：与本稿不一致处，及实现期才看见的 gap

实现分支 `refactor/unified_agent_registry`（基线 `origin/main` @ `ae39571d`；`6acfa441` 到它之间
只有 `ui/build.py` 一个 Python 文件动过，调研结论仍成立）。下面每条都是**方案没写、或方案写错、
而实现必须做决定**的地方。做了决定的写清依据，需要复核的标 ⚠️。

### 12.1 方案漏掉的事实

| # | Gap | 实现的处置 |
|---|---|---|
| 1 | ⚠️ **`"subagent"` 不只是模型面字段名，还是持久化键**：节点状态 JSON（`runner.py` 的 `_write_node_status`）、`dag_node_updated` 事件负载、`_reader.py:113` 都用它，web UI 和已落盘的运行记录读的是这个键 | D5 只改 **python 模型字段**（`node.agent`）与**模型面 schema**。持久化/事件键保持 `"subagent"`，值改成从 `node.agent` 取。改键是一次数据迁移 + 前端同步，方案没有算这笔账 |
| 2 | **dag 工具的注册条件失效**：原本"有 enabled 三方 agent 才注册"，而内置行永远在表上，roster 不可能为空 | 改为**无条件注册**。这是 D2 的必然结果，但它意味着**每个默认安装的工具表都多一个工具**——提示面变化，方案没列。⚠️ 要不要给一个 config 开关，需要拍 |
| 3 | **E2 说 `role_pool` "只留作种子"，但那样会造成反向依赖**：种子若留在 `raven/playbook/`，`raven.agent.subagent.registry` 就要 import `raven.playbook` | 种子挪到 `raven/agent/subagent/builtin_agents.py`，`role_pool.py` 与 `roles_default.yaml` **整体删除**。描述文案逐字保留（模型面文本，按 §0.5 不擅自改） |
| 4 | **playbook 的 `instance` 要不要按 run 加前缀，方案没定** | 加，和 id 用同一个 run tag。不加则同一剧本跑两次共用一个会话：有了 C6 之后不是覆盖而是排队 + 串味，比覆盖更难查。作者写 `instance: researcher` 的意思是"本次运行的这几步共用一个会话" |
| 5 | **行级 skills 与节点级 skills 的合并语义没写** | 交集，且**只能收窄**：`None` = 上一层不设限，`[]` = 一个都不给，列表 = 收窄。节点写一个行级白名单外的 skill 拿不到它 |
| 6 | **C6 的锁相对 semaphore 取在哪一侧没写** | 取在 semaphore **之内**（见 §5.2 图注）。等锁的节点会占一个并发槽，但反过来会在等槽期间一直占着句柄 |
| 7 | **(d) 降级告知从哪读 injectable、往哪投递，都没写** | 读：`AgentCapabilities` 加 `injectable_skills` / `injectable_mcps` 两个字段（默认 True，测试替身不受影响），预检只读一张表。投递：`validate_capabilities` 返回 notices，`_execute` 前置进 model_text（display 行不加，那是一行摘要） |
| 8 | **"mcps 落地前降级告知"的判据** | 不能靠 injectable——builtin 的 `injectable.mcps` 是 `True`（声明意图，§3.4）。用一个显式开关 `_capabilities.MCPS_IMPLEMENTED = False`：写了 `mcps` 就一律降级，能力落地时翻这个常量，告知自动停 |
| 9 | **"同 instance 非链头禁传"里"链头"如何判定** | 只在**该组真的有人写了 skills/mcps 时**才要求这组构成依赖链；不构成就拒（哪一个先跑没定，结果不可预测）。没人写就完全不检查——零回归 |
| 10 | **playbook 顶层 confirm 会和现存漏斗双问** | `PlaybookExecutor.execute(..., confirmed=)`：漏斗已经问过就传 `True`，dag 侧的闸不再问。J 组删漏斗时这个参数自然回到 `False` |
| 11 | **E1 的"拿不到表时不拿假清单判"没说怎么表达** | `validate_structure(known_agents=None)` = **不查 agent 名**；`raven playbook validate` 拿不到表时打印一行 note 说明没查 |
| 12 | `RoleBuildSpec` / `PlaybookExecutor(backend_factory=)` 是 E4/E5/E7 的连带删除 | 一起删了。测试侧 20 余处构造点跟着改 |
| 13 | ⚠️ **内置行的"字段级覆盖"经不起一次写盘往返**：写入路径会 validate + dump 整个 model，所以只想改 `enabled` 的那一行读回来时**每个字段都是"已设置"**——照字面应用会把种子的 description 清成 `""`，把 agent 从 roster 上抹掉 | `merge_builtin_seeds` 改为**丢掉仍等于 schema 默认值的字段**，不信 `model_fields_set`。代价是"把某字段显式写成默认值"表达不出来（`description: ""` 不是任何人想做的编辑，其余默认值都是"继承"）。**这是实现期才发现的真 bug，方案里没有** |
| 14 | **always-register dag 工具的 token 成本是可测的**：`run_subagent_dag` 的描述 + schema ≈ 5.9k 字符（其中 roster 1.2k，spawn 描述里还会再渲染一次），实测让 200k 窗口的 `available_history` 从 >130k 掉到 129.8k | 接受并记录，同时把 `test_agent_loop_token_budget` 的界从 130k 调到 129k 并注明原因。⚠️ 若不接受，需要一个 config 开关（gap 2 的同一决定） |
| 15 | **持久化侧 `"subagent"` 键要双读**：`graph.json` 是 `spec.model_dump_json()`，所以今天写的是 `agent`；而 per-node 状态项仍写 `"subagent"`（web UI 与已落盘的运行都读它） | `_reader.py` 一处同时认四种来源（entry 的两种、node 的两种），改名前录的运行仍打得开。这是 gap 1 的落地细节 |

### 12.2 已落地（本分支）

- **A**：`subagents.agents[]`（读兼容 `thirdParty`）、`BuiltinAgentConfig`、包内五条种子行、load 期同名去重 + warning、`agent_meta` 的 builtin 分支。
- **B**：`AgentRegistry`（`rows/enabled/get/meta/roster_text/descriptions/backend/names/all_names`）、builtin 工厂由 manager 注入、manager 与 dag 工具**共用一份**。
- **C**：spawn roster 含内置、stateful 判定查表、`RAVEN_LOOP_AGENT` 八处特例删除（`"raven"` 由通用 builtin 行承接）、**C6 两处 `hold_handle`**。
- **D**：D1（第二份物化删除）、D2/D3（节点可指名表上任何 name、caps 来自表）、D4（节点定义收成一份，两种 wire 拼写都留）、D5（`subagent`→`agent`、补 `skills`/`mcps`）、D6（删节点级 `confirm`）、D7（`run_with_roles` 与合成名删除，runner 改 `resolve(node)` 逐节点解析）、D8 的 (c)(d) 两条新校验、**D13 图级 `confirm`**。
- **E**：E0/E1（`NodeSpec` = `DagNodeSpec`、删 `BUILTIN_AGENTS`）、E2/E3（`role_pool` 退役）、E4/E5/E7（executor 不再建 backend）、§9 缺陷 2/4（`instance` 不再被静默丢弃；`skills: []` 语义修正）。

### 12.3 第二轮已落地

- **C1/C2/C3**：spawn 的 roster = 全表，`agent` **改必填**（enum 全表；表全空时才退回可选，避免不可满足的 schema），stateful 判定查表。C4 的措辞改了但标了 NOTE，等算法侧过。
- **F1**：`subagents.list` 多一档 `group="builtin"` + `builtin: true`；内置行的 `enabled` 是真开关。
- **F2**：连接页（`live/240-external-agents.js` + `demo/120-capabilities.js`）加"内置"分区：独立卡片，只有开关与详情，没有 connect / test / disconnect；四个 i18n 键。
- **F3**：`reject_builtin_transport_changes` 落在 `set_agents`（写入路径的唯一入口），所以 rpc / web_rpc / CLI 全部继承；`subagents.toggle` 头一次关掉内置行时**创建** override 行（只写 `enabled`）。
- **F4**：probe 对内置行直接返回 `ready / in-process`，不进批量探测。
- **F5**：`subagents.test` 对内置行不提供（页面上没有这个按钮）。
- **G1**：`subagents.agents` 键 + `get_agents/set_agents/add_agent/remove_agent`，旧键读兼容、写回统一；`AgentLoop(agents=)`、`apply_agents`（旧名保留为别名）。
- **E9**：`live_inventory()` 取代两处 `StaticInventory()`。
- **I**：`CONTEXT.md` 新增「Agent table」「Roster」两条术语；`2026-08-11-external-agent-registry-design.md` 头部标注被扩展；`2026-08-14-playbook-field-definition.md` 的 `instance` / 节点级 `confirm` / 规则 9 三行改写；`playbook-spec.md` 头部标注实现状态。

### 12.4 仍未做

- **C4/D10 的文案定稿**：`spawn` 描述已改（带 NOTE 标记）、编排指导 skill 正文未动——模型面措辞，按 §0.5 交算法侧。
- **J 组**：写下本节时整期未动。**后来做了** —— J1-J18 全部落地，见 §13；漏斗、门控、
  declines、contenders 四套机制已删除，`confirmed=` 参数也随之去掉。本行保留原判断以
  存档，实际状态以 §13 为准。
- **mcps 注入**：按方案单独一期；现在写了就降级告知（`MCPS_IMPLEMENTED = False`）。
- `test_cli_update_notice.py` 的 7 个失败**在 `origin/main` 上就存在**（stash 后复跑确认），与本方案无关。

### 12.5 第三轮（本轮）

**新发现的 gap（都已处置）**

| # | Gap | 处置 |
|---|---|---|
| 16 | **RPC 契约有第三份定义**：`SubagentRow` 同时住在 `raven/rpc/models.py`、`rpc-schema/openrpc.json`（单一真源）、以及两份 codegen 产物 `ui/src/rpc/generated.ts` / `ui-tui/src/rpc/generated.ts`。`kind` 与 `group` 都是闭合 `Literal`，`builtin` 一档过不去 | 四处同步：`kind` 加 `builtin`、`group` 加 `builtin`、新增 `builtin: bool`。`ui-tui` 的 `npm run lint:rpc` 通过（它会重算 codegen 哈希），所以手改与生成结果逐字一致。⚠️ `ui/` 的 `gen:check` 需要 `npm ci`（本机没装 node_modules），未跑 |
| 17 | ⚠️ **只在整套跑时才失败**：`test_web_rpc_config` / `test_update_subagents` 单独跑全绿，进整套后 8 个失败——`test_rpc_contract_shapes` 在同一 session 里装了校验用的 dispatcher 包装，契约违规只有那时才暴露 | 说明"单文件绿"不能当验收信号，本方案的验收必须整套跑 |
| 18 | **TUI 概览会把内置行归到「NOT INSTALLED」**：`flattenSubagentRows` 的判据是 `kind === 'openai' \|\| group === 'installed'`，内置行两条都不满足 | 加 `isBuiltin` 判据归入 `installed`；行内动作只留开关（没有连接可编辑、没有命令可测、没有行可删）；`[new]` 标签不再出现在内置行上 |
| 19 | **编排指导 skill 教的是旧 schema**（`subagent` 字段、"只有配了三方 agent 才有这个工具"），§10 已列为风险 | 与 D5 同轮改掉：字段名、roster 口径、两个新字段、示例、收尾规则。**教学部分一字未动**并在文件里留注释说明分界——那部分属算法侧 |

**验证（最终）**

```
uv run pytest tests/ --ignore=tests/integration      9355 passed, 7 failed, 33 skipped
ruff check raven tests                              All checks passed
ruff format --check raven tests                     1026 files already formatted
ui-tui: tsc --noEmit / prettier --check / eslint     clean
ui-tui: npm run lint:rpc                            OK, generated.ts in sync
ui-tui: subagentsHub 用例                            89 passed
ui: node --check（live 分片按 build.py 的顺序拼接后）    OK
```

那 7 个失败**全部**是 `tests/test_cli_update_notice.py` 的，与本方案无关：把整个工作树
stash 掉、在 `origin/main` 上单跑同一文件，失败的用例名**逐字节相同**。

未跑到的两项：`tests/integration/`（要真 LLM / 真 VM）；`ui/` 的 `npm run gen:check`
（本机没装 `ui/node_modules`）——后者校验的 `ui/src/rpc/generated.ts` 与 `ui-tui` 那份
改动相同，而 `ui-tui` 的同类校验（会重算 codegen 哈希）是通过的。

---

## 13. J 组实现记录（漏斗改"模型决定"）

J1-J18 全部落地，与 A-I 同一分支。**删掉的比新增的多**：`matcher.py` 从 192 行减到 60，
`runtime.py` 的漏斗、门控、declines、contenders 三套机制整体消失。

### 13.1 按清单

| 项 | 落点 |
|---|---|
| J1 | `loop/main.py` 的 `consider()` 拦截块删除。留下的唯一 per-turn 动作是把本轮消息交给工具（排序用） |
| J2 | `_declined` / `reset_declines` 与 inject 合并点那次 reset 一起删 |
| J3 | `_offer_contenders` 与 `contenders` 分支删。共享词表不再是"要裁决的歧义"——两张剧本一起被列出来，模型挑 |
| J4 | `gate()` / `_GATE_PROMPT` / `GateVerdict` / `MatchCandidate` / `_gate_tool` 全删。**每条提名消息省一次 LLM 调用** |
| J5 | `run_playbook` → `load_playbook`（文件改名），新增 `fills`，新增缺口回路，`mode` 不进签名 |
| J6 | `_apply_fills`：键是作者写的 plain id；**目标字段必须确实留空**，否则整体拒 |
| J7 | `MAX_GAP_ROUNDS = 2`，键是 (会话, 剧本)；派发成功后重置预算 |
| J8 | executor 把 `spec.confirm` 传成 dag 的图级 `confirm`；**asker 从构造器进**（`SubAgentDagTool(ask=...)`，接 `AgentLoop._confirm_graph`）。首稿写的 `set_ask` late-bind **被评审判成洞并删掉**：setter 意味着漏接也能跑，只是永远不问人——我的测试每处都手动接了线，于是测试全绿而**生产那个实例从来没人接**，闸是死的。构造器强制传参把漏接从一种运行状态变成建不起来 |
| J9 | `DagNodeSpec.agent` / `prompt_template` 可为空（pattern 放开 + 默认 `""`），**`validate_and_order` 兜底拒空** |
| J10/b/c/d | 新 `raven/playbook/router.py`：`select_playbooks` 按关键词命中数排序取 top-K，配置住 `playbooks.router`；**自己一份实例**，不混进 skill router |
| J11 | `disabled` = 不进 `listing()` 也不进 `names()`（enum），`[disabled]` 标记删除；CLI 走 `allow_disabled=True` |
| J12 | `listing()` 的 detail 带描述 + 参数表（类型/required/default/enum/描述）+ 留空字段清单 |
| J13 | `_missing_params_reply` → `_gap_reply`：收件人从用户改成调用方，报的是 `params.x` / `fills[...]` 而不是"回复里带个触发词" |
| J14 | 工具描述重写为正面表述（"Loading runs it"），不再提"没被自动捕获" |
| J15/J16 | 会话路径返回 guidance，CLI 由 `compose_prompt_mode=True` 保留 `_compose` |
| J17 | `raven playbook run --fill NODE.FIELD=VALUE`；不给就报"这张剧本要在运行时补值"，不静默跑缺字段的图 |
| J18 | `playbook-spec.md` 的 triggers/confirm 两节改写 + 新增 §5.1；`test_playbook_matcher` 删门控套件加 router 套件；`test_agent_loop_playbook_interception` → `test_agent_loop_playbook_entry` 整体重写；`test_playbook_executor` 的 runtime 半边整体重写；integration 的 roster 改指向注册表 |

### 13.2 J 组期间新发现的 gap

| # | Gap | 处置 |
|---|---|---|
| 20 | **剧本清单被渲染了两遍**：`IdentitySegmentBuilder` 把 listing 拼进 system prompt，工具描述里又有一份。而且 identity 那份的说明词是漏斗时代的——"用户开口就触发"、"disabled 的只能显式跑"，两句都在描述已经不存在的机制 | 删掉 identity 里的 playbook 段与 `playbook_listing` 接线。一处讲一件事，而且留下的那一处（工具描述）才装得下参数表、才能按轮收窄 |
| 21 | **"留空"必须能过 parse 才能被报成缺口**，但 `agent` 的 pattern 拒空、`prompt_template` 是必填 | 字段放开 + `validate_and_order` 兜底。分工是"文件要能载入才谈得上报缺口，图要跑不了才必须拒"——两件事在两层 |
| 22 | ⚠️ **`_blank_fields` 与 `_apply_fills` 的"可填"集合不同**：只有 `agent`/`promptTemplate` 算缺口（会挡派发），但 `fills` 还允许写 `skills`/`mcps`/`instance`（作者留白的可选项，调用方想加就加） | 有意为之，两个常量分开命名（`FILLABLE_REQUIRED` vs `_FILLABLE`）。若合并成一个，要么每张没写 skills 的剧本都变成有缺口，要么调用方补不了可选项 |
| 23 | `create_playbook` 的文案三处讲"用户开口就触发"、"enable 后自动触发" | 一起改。这个工具是写剧本的入口，它教给模型的语义要和读的那头一致 |

### 13.3 验证（A-J 全量，最终）

```
uv run pytest tests/ --ignore=tests/integration     9367 passed, 7 failed, 33 skipped
make coverage-diff  (门槛 90%)                       92.55%  (497/537 改动可执行行)  PASS
make coverage-ratchet (容差 0.05pp)                  line 79.77% (+0.39pp) / branch 69.49% (+0.33pp)  PASS
ruff check / ruff format --check                    clean (1027 files)
ui-tui: tsc --noEmit / prettier / lint:rpc          clean, generated.ts in sync
```

7 个失败全部是 `tests/test_cli_update_notice.py` 的，与本方案无关（stash 后在 `origin/main`
上单跑，失败用例名逐字节相同）。

**覆盖率门禁值得单独说一句。** 第一次跑 `coverage-diff` 是 **88.45%**，差 9 行不过。
未覆盖清单里最大的两坨恰好是这次改动里最该有测试的东西，不是边角：

| 未覆盖的 | 为什么必须补 |
|---|---|
| `tool.py` 的 `_confirmed`（810-823） | **D13 那道闸本身**。一个安全闸一行测试都没有，而它是删漏斗的前置条件 |
| `_capabilities.py` 的 (c)(d)（165-177 / 197-207） | 新增的两条校验：同句柄非链头禁传 skills 的两个拒绝分支、能力缺口的降级告知 |

所以没有去凑数，补成了 13 个真用例。闸那边覆盖的性质：`confirm=false` 不问人 / 批准才派发 /
**拒绝则零派发** / 问句必须列出每一步（一道闸管整张图，就意味着批准等于批准每一步）/
**asker 抛异常算"否"**（送不到的闸不能变成通过了的闸）/ 无问答通道照跑但记 log /
**被拒的图不计费**（闸在计费之前）。(c)(d) 那边：链头可设 skills、后续节点不行、
**只有真有人写了 skills 时才要求成链**（没写的组完全不检查，零回归）、`mcps` 由显式开关
而非 `injectable` 驱动（内置行的 `injectable.mcps` 是 true，照它读会对着没人实现的东西报成功）。

补完 **92.55%**，且 ratchet 是往上走的——之前担心"删掉高覆盖代码（门控 100%、role_pool 80%）
会把总数拖下来"没有发生。

## 14. D5 改名回退：字段回到 `subagent`

D5 把节点字段从 `subagent` 改名 `agent`（§11 那行的理由是"口径统一"）。**这个决定已回退** ——
维护者的判断是一个概念两个名字比名字不够统一更贵。

### 14.1 回退的是哪一层

| 层 | 改名前（!130） | 现在 |
|---|---|---|
| 节点字段（模型面 JSON Schema、playbook 文件） | `agent` | **`subagent`** |
| `spawn` 的参数 | `agent` | **`subagent`** |
| 落盘的节点状态、`dag_run_started` / `dag_node_updated` 负载、两个前端 | `subagent`（从未改过） | `subagent` |
| 配置里的那张表 | `subagents.agents[]` | `subagents.agents[]`（**不动**） |
| `AgentRegistry` / `AgentRow` / `AgentCaps` | 表相关的类名 | 不动 |

**容器与角色是两个轴,各留各的词**:表列的是身份（`agents[]` 里的行），字段说的是"这一步由谁以子身份跑"
（`subagent`）。改名回退只统一了后者 —— 同一个对象在 schema 里和磁盘上不再有两个名字。

### 14.2 为什么这个方向便宜

D5 那次改名之所以看着"更统一"，是因为它对齐了容器名；但它**打破的是更老的那个拼法** ——
`agent` 是 playbook 文件侧一直在用的名字（见 `playbook/types.py` 的历史说明：
"the step's target field was spelled `agent` here and `subagent` there"），
而 `subagent` 是落盘与线上契约侧一直在用的名字。

两个方向的代价不对称：

- **统一到 `subagent`**（本次）：改模型面 schema + 作者面文件 + 一条存量剧本迁移。
  **线上契约零改动、`generated.ts` 零改动、两个前端零改动、无数据迁移** —— 因为落盘键本来就是 `subagent`。
- 统一到 `agent`（不做）：要改 `DagRunStartedNode` / `DagSnapshotNode` 两个 `extra=forbid` 的 wire 模型、
  重新生成契约与两份客户端、改两个前端。破坏性契约变更，换来的只是命名整齐。

### 14.3 迁移

存量剧本**全部**写的是 `agent:`（不只是 !130 之后写的 —— 那是 playbook 侧一直的拼法），
所以 `_migrate_legacy_nodes` 里这条改写是**无条件**的，不挂在任何标记上：节点带 `agent` 且不带
`subagent` 时改写键名。`extra="forbid"` 意味着不迁移就是静默失效 —— 剧本加载报 warning、
从库里消失，正是本方案评审抓到的那类 bug。

`_reader.py` 保留一条 `agent` 兜底，覆盖 !130 那一个版本期间写下的 `graph.json`。

## 15. `skills` / `mcps` 从 dag 工具参数上撤下

维护者定的：**模型组图时只该关心传什么 prompt**。这两个字段从 `run_subagent_dag` 的参数里删除，
playbook 侧保留，由**引擎在派发那一步消费**。

### 15.1 改了什么

| 层 | 之前 | 现在 |
|---|---|---|
| `_NODE_SCHEMA`（模型读的工具定义） | 8 个字段 | **6 个** —— `id` / `subagent` / `promptTemplate` / `dependsOn` / `inputs` / `instance` |
| `DagNodeSpec`（数据模型） | 8 个字段 | **不动** —— playbook 文件解析成它，字段必须在 |
| executor 派发 | 把 `skills` / `mcps` 原样传给 dag | `skills` 折进这一步的 `promptTemplate`（一句建议）；`skills: []` 与 `mcps` 走 note，不进 prompt |
| playbook 校验规则 9 | 同句柄非链头禁写 `skills` / `mcps` | **删除** |

省下的模型面成本：节点 schema 从 3019 字符降到 2516（约 **148 token / 每次请求**），
外加不必再教模型 `None` / `[]` / 列表 那套三值语义。

### 15.2 `skills` 的语义由维护者定死了：一句建议

> "这里所谓的 skills 其实只是注入到节点 agent 的上下文，告诉他这些 skills 可以优先用，仅此而已，
> 没有只能看到哪些或者一定要用哪些的说法。"

所以它折进这一步的 `promptTemplate`，随任务描述一起交给跑这一步的 agent，**既不是过滤器也不是命令**：

| | 表达得出吗 |
|---|---|
| "这几个跟这活相关，优先考虑" | ✅ 这就是它 |
| "只让这一步看见这几个技能" | ❌ 菜单不再被过滤，其余技能照样在它眼前 |
| "这一步必须用某个技能" | ❌ 用哪个始终是那个 agent 现场按上下文定的 |

原来的 `skills_allow` 是**菜单过滤器**——决定哪几条 skill 摘要写进 subagent 的 system prompt
（`raven_loop.py` 的 `build_subagent_prompt`）。那条路的机制**一行没删**，只是不再有人往里喂：
模型面没有这个字段，executor 改成折进 prompt。想把两者都接上是可行的（且对模型零成本），
但那需要区分"开会话的那个节点"——resume 时整条沿用历史里的 system prompt，收窄在续会话的节点上
静默失效。按上面这个语义，不需要那条路。

`skills: []` 因此**没有含义了**：它既不是"只看见零个"（限制），也不是"禁止用"（命令）。
按不写处理，**但派发时明确回报一句**——文件里写着 `skills: []` 看起来是提了要求的，
静默丢弃就是这次一路在修的那类 bug。真要"这一步别用技能"，写在 `promptTemplate` 里。

### 15.3 `mcps` 为什么不拼进 prompt

维护者说的是"把相应的东西拼进 prompt"，这一条我偏了，理由是它拼不出东西来：
subagent 的工具注册表是一份写死的七个（`raven_loop.py` 起：读/写/改文件、列目录、执行命令、
网页搜索、网页抓取），**没有任何 mcp 接入路径**。把 `mcps: [github]` 拼成"你可以用 github"，
它手上没有那个工具，只能说做不到或者编 —— 那正是本方案评审判定的假承诺。
所以它保持"声明 + 明确回报未生效"，等 mcps 注入那一期真建出来再说。

### 15.4 规则 9 为什么必须跟着删

它的全部依据是"resume 时整条沿用历史里的 system prompt，所以只有链头的 `skills` 生效"。
`skills` 现在落在 `promptTemplate` 上，那是**每个节点都追加的 user message**，
链上任何一个节点写了都同样到达那一步的 agent。依据消失了，规则留着就变成一条会拒掉合法图的假规则。

### 15.5 留下的一处死角

dag 侧的 `skills` 处理还在（`_resolve_node` 的 per-node 窄化、`_check_injection_on_continuation`、
`_injection_notices`）。模型不再被告知这个字段、executor 不再传它，所以**没有生产路径会喂到它**，
它今天只被测试直接调用。删掉是净收益，但会连带动 `_NodeBuild` / `Injectable` / `MCPS_IMPLEMENTED`
一串，本轮没做，记在这里。

