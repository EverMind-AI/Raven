# `mode: stint` -- 在 playbook 和 DAG 上实现多轮角色循环

日期：2026-09-17
状态：**提案**，已落地。本文保留提案时的原貌，作为设计记录。
相关：
- `2026-08-14-playbook-field-definition.md` / `playbook-spec.md`（playbook 字段）
- MR !647 `feat(playbook): write each turn its own table of sub-agent workers`

> **落地后改了名字，正文没改。** 读这份文档时按下表换算：
>
> | 文中写的 | 实际是 |
> |---|---|
> | `hstar/`（项目目录） | `.stint/` |
> | `hstar_guard_{role}.md` | `stint_orders_{role}.md` |
> | `raven hstar task/ask/confirm/init` | `raven playbook stint task/ask/confirm/init` |
> | `raven/hstar/*.py`（被继承的模块） | `raven/stint/*.py` |
> | `mode: rounds`（本文一直这么写） | `mode: stint` |
> | 「一次运行」叫 plan | 叫 **stint**，`raven playbook stints …` |
>
> 文中引用的 H\* 运行时本身已经整体下线；它留下的设计结论在
> `docs/plans/2026-09-18-stint-acceptance.md` 末节。

---

## 0. 一句话

把 H* 的「Planner -> Developer -> QA，跑 T 轮」搬到 playbook 上：
**一轮 = 一张 DAG，多轮 = DAG 后台链上的一个新分支**，入口仍然只有 playbook 一个。

三层没有一层是新建的。

---

## 1. 目标与约束

| | |
|---|---|
| 目标 | 在现有 DAG + playbook 上表达 H* 式的多轮角色循环 |
| 约束 1 | **只有一个对外接口** —— playbook（`load_playbook` / `raven playbook run`） |
| 约束 2 | **不新建执行层** |
| 约束 3 | **循环体是数据不是代码** —— 不引入任何求值器 |
| 容差 | 与 H* 九成相似即可，那一成落在最划算的地方 |

---

## 2. 架构

```
描述层   playbook 文件  mode: stint                    唯一对外接口
            |
            |  executor 认出它 -> 启一个 plan，立刻返回 run_id
            |  （executor 仍然 stateless / nothing waits）
            v
执行层   DAG 后台 task 链 + plan.json
            |
            |  每轮结束 -> 判停止条件 -> 不停就编译下一轮、不 announce
            v
编排层   每一轮 = 一张图 -> SubAgentDagTool.execute
            |
            |  校验 / 配额 / 调度 / 五种后端 / 回注   全部复用
            v
执行体   子 agent（进程内 / ACP / CLI / HTTP）
```

**关键**：`rounds` 不是 `dag` 的兄弟，是它的**外层**。一次扇出 = `mode: dag`；
多轮 loop = `mode: stint`，而每一轮内部就是一张 dag。嵌套，不是并列。

---

## 3. 十条核心决定

| # | 决定 | 依据 |
|---|---|---|
| 1 | 多轮循环挂在 `dag_tool._run_detached` 的一个**新分支**上，不新建执行层 | 先例是 `replanned_into` —— 「一张图跑着变成另一张图、不回注主 agent」这个机制**已经存在** |
| 2 | **`roles[]` 就是 `delegate[]`**，同一个结构 | `mode: agent` 和 `mode: stint` 共用一份工人描述 |
| 3 | **轮内不做无限 continue**，只允许有上限的 handback（默认 2） | 换掉整套 handoff + `role_compaction` + 对话簿记 |
| 4 | **轮间交接靠 append-only 的 journal 文件**，不靠对话 | 每轮新对话，H* 那条「单个 Developer 时对话本身就是记忆」的退路不存在 |
| 5 | **文件强制四层**：声明 -> prompt 注入 -> 事前拦 -> 事后撤 | 比 H* 现在多一层（`charter.checks` 是事前） |
| 6 | **`read: soft` / `write: hard`** | 照抄 H*。限制读会让角色读不到它需要的上下文，而读没有副作用 |
| 7 | 模型 / MCP / workspace 靠 **delegate 行的协议参数** | v3 规格现成，待落地。**没有它，三个角色只剩 prompt 的差异** |
| 8 | **长 prose 留在文件里**，playbook 只有骨架和槽 | 实测 2026-09-02：逐字塞任务规则的图顶到回复上限 |
| 9 | **不引入脚本 / 求值器** | `roles[]` 扁平、只有依赖边、没有条件没有变量 —— 与 `nodes[]` 同一套受限程度 |
| 10 | DAG 走「**可被任何进程重新拾起**」，不是「活得更久」 | 状态已经具名落盘，不需要哈希链 |

---

## 4. Schema

```yaml
version: 1
mode: stint
name: game-dev
description: 按 backlog 逐轮推进一个游戏项目

memory:
  - {path: hstar/backlog.json}
  - {path: JOURNAL.md, append: true, recentRounds: 2, maxChars: 16000}

verify:
  - {name: build, run: "godot --headless --export-release"}
  - {name: tests, run: "uv run pytest -q"}

roles:                                    # 结构 == delegate[]
  - as: planner
    name: Raven-Research
    model: anthropic/claude-opus-5        # 协议参数（待落地）
    owns:    [hstar/AGENT_DECISIONS.md, "reports/brief_{NN}.md"]
    appends: []
    enforce: {read: soft, write: hard}
    playbook:
      capability: {tools: [read_file, write_file]}
    promptTemplate: |
      {{ref:hstar/prompts/planner.md}}
      {{round.guard}}
      ## 第 {{round.index}} 轮
      {{round.journal}}

  - as: developer
    name: Raven-Code
    dependsOn: [planner]
    workspace: ./
    owns: ["**"]
    journalSection: Dev
    verifyAfter: [build, tests]
    maxHandbacks: 2                       # 上限，提交时可数
    playbook:
      capability: {tools: [read_file, write_file, exec]}
      action:
        checks:
          rules:
            - {tool: write_file, forbid: "hstar/HUMAN_DECISIONS.md",
               message: 那是人的文件}

  - as: qa
    name: Raven-Review
    dependsOn: [developer]
    mcps: [godot]
    owns:    ["reports/round_{NN}.md"]
    appends: [hstar/backlog.json]
    playbook:
      capability: {tools: [read_file, write_file, exec]}
      action:
        checks:
          rules:
            - {tool: write_file, forbid: "src/", message: QA 不修代码}

stop:
  maxRounds: 30
  until: verdict
```

### 新增的轮级槽

现有占位符是两类：编译期 `${params.x}`，运行期 `{{dep.output}}` / `{{ref:路径}}` / `{{inputs.<k>}}`。
多轮加第三类：

| 槽 | 内容 |
|---|---|
| `{{round.index}}` | 第几轮 |
| `{{round.journal}}` | 最近 N 轮的 journal 片段（已裁剪） |
| `{{round.verify}}` | 上一轮 verify 的结果表 |
| `{{round.guard}}` | 从 `roles[]` 的 `owns`/`appends`/`enforce` 渲染 |

---

## 5. 一轮里发生什么

```
plan.json 记着：round = 12，上一轮 run_id，停止条件
     |
     v
编译第 12 轮的图（roles[] -> nodes[]，槽填好）
     |
     v
SubAgentDagTool.execute   提交阶段全部复用：
     |                    Kahn / 引用 default-deny / id 认领 / 能力预检 / 配额 / confirm
     v
ready-set 波次跑三个节点
     planner  --> developer --> qa
     |             |
     |             +-- verifyAfter: 跑 build/tests
     |                 挂了且 handbacks < 2 -> 把失败递回去，重来一次
     |
     +-- 每个节点跑完：git diff -> 越界的挪进 violations/ 并撤销
     |
     v
_run_detached 收尾
     |
     +-- 这个 run 属于一个 plan？
     |     |
     |     +-- 是，且没到停止条件 -> 编译第 13 轮并提交，**不 announce**
     |     |
     |     +-- 是，到停止条件了   -> announce 整个 plan 的汇总
     |     |
     |     +-- 否（普通 DAG run）  -> 走原来那条路
```

**主 agent 全程不在场**，只在起跑时提交、结束时收汇总。

---

## 6. 文件强制：四层

H* 现在是三层，本方案多一层。

### 层 1 -- 声明（playbook）

```yaml
owns:    [hstar/AGENT_DECISIONS.md]     # 只有这个角色能写
appends: [hstar/backlog.json]           # 只能加，不能删
enforce: {read: soft, write: hard}
```

对应 H* 里 guard 文件的 YAML frontmatter（`bootstrap.py init` 生成的那份）。
**声明写一次，两处读**：prompt 注入 + `_enforce`。

### 层 2 -- prompt 注入（`{{round.guard}}`）

H* 的 `guard_section()` 定了主次：

> runtime 自己的文本是**契约** —— 这个角色必须产出什么、永远不能碰什么；
> guard 是**这个项目对它的解读**，而且是两者中**更具体的那个**。

### 层 3 -- 事前拦截（`charter.checks`，!647 已有）

```yaml
action:
  checks:
    rules:
      - {tool: write_file, pathPrefix: ./out/a/, message: 只能写 out/a/ 下面}
```

在 worker **自己的进程**里、`ToolRegistry.execute` 里跑，**调用发生之前就拒**，
拒绝理由回到它自己的消息列表，它下一轮自己改。**H* 没有这一层。**

### 层 4 -- 事后撤销（搬 H* 的 `_enforce`）

三档：`OWNS` 放行 / `APPENDS` 只能加（删了就 trim 回去）/ 其余 -> 撤销。

**必须搬对的一处细节**：

```python
touched = git.touched_since(stage_base)     # 不是 git.changed()
```

> 没有 `stage_base` 的话，一个**把越界写入 commit 掉**的角色就从 `git status` 里消失了，
> 这一遍什么也找不到 —— 于是边界对**任何会用 git 的角色**都变成了建议。

### 为什么两层都要

层 3 只拦得住走 raven 工具的写入。worker 一句 `exec("echo x > file")` 就绕过去了。
层 4 什么都撤得掉但只能事后。**双保险，缺一不可。**

---

## 7. 轮间交接

同一个角色跨轮的交接（不是 planner -> dev，是 dev -> 下一轮的 dev）。

H* 的做法，直接照搬：

```
JOURNAL.md   append-only，每轮一节
             JOURNAL_ROUNDS_IN_PROMPT = 2   只有最近 2 轮进 prompt
             MAX_JOURNAL_CHARS = 16_000     第二道闸
             第一轮：「(nothing yet: this is the first round)」
             更早的轮次在 git commit log 里
```

**但有一条判断在本方案里翻转了。** H* 的 `dev_handoff_each_round` 默认不开：

> 单个 Developer 时**对话本身就是记忆**，每轮关掉它是在为一份没人需要的交班条付钱。
> 多实例时对话不是存放这一轮的安全地方 —— 哪个实例接哪一份活，轮与轮之间会变，
> **所以一个实例学到的东西必须写下来才能到达下一个**。

本方案**每轮都是新对话**（不复用 instance），所以「对话本身就是记忆」这条退路不存在：
**每轮都必须写交接，没得选。**

表达：`memory[].append/recentRounds/maxChars` + `roles[].journalSection`。
这不是新机制，是一个 append 约定 + 一个 prompt 组装时的裁剪规则（搬 `journal_tail`）。

**比 handoff 好的地方**：它是文件，人能读能改；handoff 是对话内部的东西，看不见。

---

## 8. Prompt 架构

H* 现在：**23 个模板 / 1039 行**（en，zh 对称）。

```
角色主模板   hstar_{planner,developer,qa}.md          68/86/89 行   老的 KANBAN 路线
board 变体   hstar_board_{planner,developer,qa}.md    60/91/71 行   新的 backlog 路线
guard 模板   hstar_guard_{planner,developer,qa}.md    97/150/134 行
continue     5 个 + handoff 2 个
零碎         environment / question / resume_note / instance / checks / request
```

### 砍（约 10 个）

| 砍什么 | 为什么 |
|---|---|
| `hstar_{planner,developer,qa}.md` + 对应 continue | **两套路线要收敛**。`board.py` 自己记了教训：*"A role given two systems in one prompt uses neither well."* 留 board/backlog 那套 |
| `hstar_*_continue.md` x 5 | 决定 3：轮内不做无限 continue |
| `hstar_handoff.md` / `hstar_handoff_ask.md` | 不 continue 就不会超预算，整套交班机制不需要 |
| `{{done_sentinel}}` 那一段 | dispatch 返回就算这一轮结束，不需要哨兵 |

### 搬家但不塞进 YAML

```yaml
promptTemplate: |
  {{ref:hstar/prompts/developer.md}}      # 长 prose 留在文件里
  {{round.guard}}
  ## 第 {{round.index}} 轮要做的
  {{planner.output}}
  ## 前几轮你自己的记录
  {{round.journal}}
```

与 `incomplete_hint` 那条建议一致：**图携带引用，不携带正文**。

### 不动

`hstar_guard_*.md` 三份（381 行）是项目特定的守则，`bootstrap init` 照样生成 ——
变的只是 `enforce`/`owns` 那份 frontmatter 的**来源**：从 guard 文件里的字段，
变成 playbook 的 `roles[]`，init 时反向渲染出 guard 文件。

`hstar_environment.md` / `hstar_question.md` / `hstar_developer_checks.md` /
`hstar_planner_request.md` 都是小片段，照搬。

**收敛后**：23 个 / 1039 行 -> 约 12 个 / 700 行。

---

## 9. 改动清单

### 批 0 -- 先决（互相独立）

| # | 改什么 | 大小 |
|---|---|---|
| 0.1 | **DAG 图级续跑** —— 读回图、跳过 completed、其余重跑；顺带把仲裁 desk 落盘 | 中，**本来就欠** |
| 0.2 | **`PlaybookSpec.mode` 扩开** —— 加 `agent` / `rounds` | **一行** |
| 0.3 | **delegate 协议参数落地** —— `model` / `mcps` / `workspace` | 中，**规格现成**（见待拍板 A） |

### 批 1 -- 核心

| # | 改什么 | 大小 |
|---|---|---|
| 1.1 | `RoundsSpec` schema + 校验（`roles`/`memory`/`verify`/`stop`） | 中 |
| 1.2 | `plan.json` + `_run_detached` 的新分支 | 中 |
| 1.3 | executor 的 `rounds` 分支：启 plan，返回 run_id | 小 |
| 1.4 | 轮级槽 `{{round.*}}` | 小 |

### 批 2 -- 搬 H*

| # | 改什么 | 来源 |
|---|---|---|
| 2.1 | `_enforce` 事后 git diff + 撤销（含 `touched_since(stage_base)` 那个坑） | `hstar/runtime.py` |
| 2.2 | `verify` 跑命令 + handback | `hstar/checks.py` |
| 2.3 | `journal_tail` 窗口裁剪 | `hstar/memory.py` |
| 2.4 | git 分支隔离 `plan/<run-id>` | `hstar/history.py` |

### 批 3 -- 收敛

| # | 改什么 |
|---|---|
| 3.1 | prompt 从 23 个收到约 12 个（en/zh 同步） |
| 3.2 | 前端的取消与介入入口 |
| 3.3 | 配额：从内存计数器改成落盘账本（跨进程 + 重启不清零）。**建议单独一个 PR** |

---

## 10. 跟 H* 的三处差异

| # | 差异 | 性质 |
|---|---|---|
| 1 | 轮内最多 N 次 handback，不做无限 continue | **妥协**，换掉整套 handoff |
| 2 | 多一层事前拦截（`charter.checks`），事后撤销照留 | **改进**，双保险 |
| 3 | DAG 后台链 + 续跑，不是 detached 长跑进程 | **改进**，可被任何进程重新拾起 |

---

## 11. 主 agent 在哪

**在，而且角色跟现在的 DAG 完全一样 —— 只是等得更久。**

它只在**两端**出现：起跑时理解意图、选 playbook、填参数；收尾时解读结果。
中间完全缺席 —— 这是对的，长跑任务不该占着主会话。

注意：这其实是**给 H\* 加了一个主 agent**，不是去掉。H* 现在是 CLI 起的，根本没有。

**但「挂起问谁」这条还空着**，见待拍板 D。

---

## 12. 风险点（按严重度）

| # | 风险 | 说明 |
|---|---|---|
| **1** | **`_run_detached` 加分支** | 它在**所有** DAG run 的收尾路径上，现在已有四个分支（`replanned` / `outbox` / `cancelled` / `announce`），每个都有注释说明为何不能调换顺序。加第五个，漏一种组合就是「跑完了谁也没收到」或「announce 了两次」 |
| **2** | **配额语义冲突** | 现在「一次 run 记一次」，防的是「结果注回 -> 新一轮 -> 再提交更多活」这个**没有用户输入的环路**。而多轮 plan **正好就是那个环路**，只是被授权了。记 30 次还是 1 次？记 1 次等于给这条防线开口 |
| **3** | **长跑 plan 饿死主会话** | 共享信号量默认 8。30 轮的 plan 长期占额度，主会话的 `spawn` 排不上队。H* 现在**不进池**所以没这问题，进池之后就有 |
| **4** | **续跑要能从「第 12 轮跑一半」恢复** | 不只是「第 12 轮没跑」。后台 task 链挂在网关进程里，任何一次重启都断 |
| **5** | **协议参数是地基** | 三个角色靠 `model`/`mcps`/`workspace` 区分。若批 0.3 答案是「砍了」，要退回「注册三个 agent row」，「一个工人描述三处复用」就立不住 |
| **6** | **H\* 与新实现并存** | `board.py` 的教训：两套并存，角色两个都用不好 |
| **7** | **`verify` 的 `run:` 是执行面** | playbook 是分发单元。别人给的 playbook 带 shell 命令，现在没有闸挡着。得进 `confirm` 清单，或限制只有内置 playbook 能带 |
| **8** | **git 分支与主会话 checkout 并发** | worktree 是答案，但那是新东西 |

**最该盯 1 和 2** —— 前者会静默出错，后者会开安全口子。

---

## 13. 对现有流程的影响

| 改动 | 影响面 | 判断 |
|---|---|---|
| `PlaybookSpec.mode` 加值 | 无 | 现有 `dag`/`prompt` 分支不动 |
| `RoundsSpec` schema | 无 | 全新字段 |
| executor 的 `rounds` 分支 | 无 | 新分支 |
| 图级续跑 | 无 | 现在是「不能续」，加上是「能续」 |
| `_enforce` git diff | **要小心** | 必须只在声明了 `owns`/`appends` 的角色上跑。条件写错，**每个 DAG 节点都打 git 快照** |
| `verify` | 无 | 只在声明了的角色上跑 |
| **`_run_detached` 加分支** | **所有 DAG run** | 判断「属不属于一个 plan」要读盘，加在热路径上，且这个判断不能失败 |
| **`DelegateEntry` 加协议参数** | **!647 的生成路径** | `CamelBase` 是 `extra="forbid"` —— 字段加到模型上，**模型生成的表也能带了** |
| **配额改落盘账本** | **所有派发路径** | `spawn` / DAG / 直聊都读它。影响面最大 |
| prompt 收敛 | 只影响 H* | en/zh 各 23 个要同步 |

### 一条容易漏的

`mode: agent`（!647 的用工表）和 `mode: stint` **共用 `DelegateEntry`** ——
这是方案的优点，也意味着改它会同时影响两条路。而 **`mode: agent` 是每个 turn 都跑的**
（开关打开时），在最热的路径上。

**所以给 `DelegateEntry` 加字段的风险不在 `rounds`，在 `agent`。**

保险做法：协议参数**只在文件加载路径上解析，生成路径显式拒绝**。现有机制刚好支持 ——
`agent_generator.py` 的 schema 是**手写的**，不从 pydantic dump。
但要**加一个测试钉死这条**，否则哪天有人把 schema 改成自动生成，口子就开了。

---

## 14. 明确不做

| | 为什么 |
|---|---|
| 脚本层 / JS 引擎 / AST 求值器 | 安全关键组件 + 丢掉「提交时可校验」。受控构造能拿八成表达力 |
| `when` 的通用表达式 | 同上 |
| 嵌套展开 | 规模不可预估 -> 审批点名和配额同时失效 |
| 给 `nodes[]` 也开一个 `model` 口子 | 同一件事的第二个表达位置，立刻要回答「冲突了听谁的」 |
| 把 `nodes[]` 和 `roles[]` 合并成统一 `units[]` | 收益（少一个概念）不抵代价（重写一个有 14,914 行测试钉着的子系统） |
| 长周期层做成 daemon | 已有 cron 负责「醒来」 |
| 静默截断 | 读起来像「全都覆盖了」，其实没有 |

---

## 15. 待拍板

| # | 问题 | 卡在哪 |
|---|---|---|
| **A** | v3 设计第 2 节那张**协议参数表**，砍了还是漏回写了？ | 批 0.3。运行时的 `Worker` 只有 `label`/`agent`/`brief`/`charter`/`payload`，生成器 schema 里 `model`/`mcps`/`workspace` 零命中。**这是地基** |
| **B** | H* runtime 怎么处理：**并存 / 改造成 rounds 的实现 / 保留为 game 专用**？ | 影响批 2 是「搬代码」还是「改调用方」 |
| **C** | `maxRounds` / `maxHandbacks` 的默认值 | 影响配额预估和审批文案 |
| **D** | **多轮里节点挂起要人裁决，问谁？** | DAG 现在回注主会话（但那时主会话可能已被 compact、甚至关了）；H* 写 `NEEDS-HUMAN` 绕开、不阻塞，人有空了通过 RPC 回答、排队到下一轮。**倾向 H\* 那条**，但要保留 RPC 通道。这条决定 `mode: stint` 是「会停下来等」还是「永远往前走」 |

---

## 附：为什么不是别的形状

| 想过的 | 为什么不 |
|---|---|
| 在 playbook executor 里加 `for` | `executor.py` 明写 stateless / nothing waits，加循环直接推翻它，然后要自己处理跑到第几轮的持久化、重启恢复、进程归属 —— 而这些 H* 已全做完 |
| 新开一个 `start_hstar_run` 工具 | 违反「一个对外接口」的约束 |
| 让 H* runtime 读 playbook（保留 detached 进程） | 可行，但进程模型是三层里最弱的一环（网关重启即断），而 DAG 的答案应该是「可被重新拾起」 |
| 把 H* 做成 DAG 的一种「循环节点」 | 打破「提交时拓扑可数、可逐条审批」，且拿最弱的一环（DAG 不能续跑）去承长周期的重 |
