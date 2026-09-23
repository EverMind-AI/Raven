# 技能、记忆与扩展

本页面向使用者。实现扩展请继续读[构建插件](building-plugin.md)或
[构建 Agent](building-agent.md)；连接已有产品请读[Agent 集成](agent-integrations.md)。

指令、工具、记住的事实和 Agent 是不同类型的能力。应选择能补足当前任务缺口的最小扩展。

## 能力地图 { #capability-map }

| 组件 | 提供什么 | 不代表什么 |
| --- | --- | --- |
| Skill | 可发现的操作过程，可附脚本和参考文件 | 新的运行时工具或无限制执行 |
| SkillForge | 检索本地技能、记忆中的经验和 Skill Hub | 自动修改源代码 |
| Memory Engine / EverOS | 跨对话持久化与召回 | 所有历史消息永远在模型上下文中 |
| MCP 服务 | 外部服务提供的工具、资源或 prompt | 独立 Agent 会话 |
| Plugin | 工具、hook、服务、记忆后端等运行时贡献 | 与宿主进程隔离 |
| 专用 Agent | 自身身份、配置、工具和会话行为 | 自动访问全部宿主工具或凭据 |
| [Playbook](playbooks.md) | 命名的可复用过程或图 | 被 SkillForge 索引的 skill |
| [Evolver](evolver.md) | 独立的、由基准驱动的 harness 改进评估 | 生产 Agent 在对话中改写自身代码 |

装配边界见[架构](architecture.md)，多 Agent 协作见[DAG 编排](orchestration.md)。

## 发现与管理技能 { #discover-and-manage-skills }

先检查当前安装提供哪些能力：

```bash
raven skill list
raven skill get --help
raven skill block --help
```

SkillForge 有三个来源：本地文件、通过配置的记忆后端召回的技能/案例，以及远程
Skill Hub 候选。可用性取决于配置和服务连通性；目录搜索命中不证明 bundle 已安装，
也不证明所需工具存在。

`skillForge.discovery` 默认 `pull`：在符合条件的轮次中提供简短的名称/描述菜单，
模型用 `find_skill` 搜索、用 `read_skill` 读取正文。`push` 则运行选择/注入管线，
把选中正文放入上下文。Pull 避免 push 路径逐轮的重写与门控模型调用，但不意味着所有
检索都无成本，也不意味着每轮获得相同菜单。

Bundle 下载有独立的同意设置：

```json
{
  "skillForge": {
    "discovery": "pull",
    "autoInstall": "prompt"
  }
}
```

`autoInstall` 默认 `auto`；`prompt` 在交互式终端请求同意，没有 TTY 时表现为 `off`。
`off` 跳过 bundle 下载，但**不禁止**读取已通过检查的 skill 正文。需要禁用技能本身时
应使用 blocklist。执行前检查脚本；目录安全分数不是安全保证，当前策略也不会仅因缺少
分数就直接拒绝。成功安装记录在 `<workspace>/skills/hub/installs.jsonl`。

## 记忆与学习边界 { #memory-and-learning-boundaries }

EverOS 是默认记忆后端插件，与 Raven core 独立发行，通过 Memory Engine 召回用户
上下文和 Agent 经验。用 `raven plugins` 检查当前后端，并通过 onboarding 与部署的
插件设置配置记忆。

应区分三个机制：

1. 上下文组装决定本次模型调用看到什么，长历史可能被归档或压缩。
2. 记忆存储和召回把选定知识保留到对话之外。
3. 经验提炼是后端相关能力，与 SkillForge 检索不同。当前树保留了
   `skillForge.extraction` 配置和关于本地管线的旧描述，但实际宿主装配不消费该设置。
   不要假定开启它就会启动本地技能提炼或创建 `.cache/skills.db`。

反馈驱动的 skill 版本管理和退役，并不会因为存在配置占位字段就自动生效。
宣称学会某个过程前，应检查记忆后端实际存储的 case/skill 和健康情况。Harness
自进化由独立 Evolver 工具执行，并经过基准评估，先读
[Evolver：使用与实验](evolver.md)，实现细节见[设计与实现](self-evolution-map.md)。
不要把检索或后端经验提炼描述为自动修改生产代码。

## 从已完成任务到可复用知识 { #from-a-completed-task-to-reusable-knowledge }

先决定什么需要保留：

| 信息 | 合适位置 | 如何确认 |
| --- | --- | --- |
| 用户偏好或个人上下文 | 记忆后端 user track | 检查存储记录，在新对话测试召回 |
| Agent 经验或可复用案例 | 后端支持的 agent track | 查看后端实际返回的 case/skill |
| 已审核操作指令 | 本地 Skill | 阅读正文，检查所需工具 |
| 可重复多步骤执行 | [Playbook](playbooks.md) | 校验并测试保存的图 |
| 来源文档与段落 | [知识库](knowledge.md) | 核对索引和命中原文 |
| 失败运行证据 | [Trajectory bundle](trajectory-debugging.md) | 回放并保留相关工件 |

EverOS 分别提供 user 与 agent 召回：用户记忆供应个人上下文，Agent case/skill 可作为
SkillForge 候选。希望连接的对话应保持预期 `userId` 和 `agentId` 稳定。
不同 Agent 身份或记忆后端不自动共享同一记忆集合。

可用不敏感偏好测试，例如“我喜欢先给简短清单，再写操作说明”。完成轮次，
检查可用的记忆视图/后端记录，再以同一身份开启新对话并询问相关问题。
确认记录和检索上下文，而不只相信模型说“我记得”。若在测试问题中再次写出偏好，
就不能证明发生了召回。

需要复用工作经验时，先完成并验证一个小任务，再检查后端是否记录有用的 case/skill。
若没有，显式编写审核本地 Skill，或[创建 Playbook](playbooks.md#inspect-and-create)。
不要把每次成功回答都当成自动安装了新流程。

宿主记忆写入在轮次之外排队，有队列上限和重试。回答完成不意味着后端索引完成；
故障、队列限额和停机可能使数据未索引，或处于无法确认结果的 in-flight 状态。
记忆持久化不是完整 transcript 备份。

## 上下文压力不等于长期学习 { #context-pressure-is-not-long-term-learning }

Curator 为下一上下文窗口选择历史。压力较低时走不调用模型的 fast path；
slow path 可消耗有限模型调用生成计划，无有效计划时使用确定性 fallback。
归档消息可在磁盘原样保存、后续取回，但不一定出现在每个 prompt 中。

轮内 **Compaction** 是另一机制：可以裁剪旧工具结果正文、总结 transcript 头部，
保留最近消息。默认关闭，不写长期记忆笔记。显式启用示例：

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "enabled": true
      }
    }
  }
}
```

请合并而不是覆盖其他 Agent 配置。Compaction 可调用本轮模型并产生费用，
不会扩大 provider 的真实上下文上限。Curator 和 compaction 都不能替代明确任务说明
或可重新读取的来源文件。

## 排查缺少召回或技能 { #diagnose-missing-recall-or-skills }

| 现象 | 检查 |
| --- | --- |
| 新对话没有偏好 | 后端健康、索引完成情况、身份与召回相关性 |
| Skill 已列出但未使用 | 正文是否读取、所需工具、blocklist 和当前 Agent |
| 任务完成后没有新技能 | 后端实际提炼支持；`skillForge.extraction` 本身不启动宿主管线 |
| 长任务丢失细节 | 归档与当前窗口的区别、compaction、可重新读取的来源 |
| 模型成本增加 | 后端提炼、embedding、Curator slow path，以及启用时的技能 rewrite/gating |

记忆视图可能暴露私有对话和推断偏好。应另行检查后端的删除与保留控制；
删除宿主 session 或屏蔽 skill 不证明所有后端记录也已删除。

## 扩展工具或构建 Agent { #extend-tools-or-build-an-agent }

能力已由服务接口提供时使用 MCP。配置服务、核对工具列表与认证，再先测试只读操作，
最后才允许修改。`raven plugin auth <server>` 处理已配置 MCP 的 OAuth，不是任意
插件安装器。

需要进程内工具、hook、服务或记忆后端时使用 Raven plugin。声明式
`raven-plugin.toml` 列出贡献，代码运行于宿主信任域内。`raven plugins` 列出已安装
贡献。插件 Tool Gate 与平台 Permission Gate 是不同契约。

需要独立身份与执行配置时，使用 `raven agents new <name>` 创建专用 Agent 骨架。
先读 `raven agents new --help`；生成目录包含自己的 README。源码中的
`agents/BUILDING.md` 说明 launcher、配置、插件工厂及发现机制。运行状态不要写进
定义目录，要区分 Agent 的 state root、ACP home 与用户工作目录。

## 运行完整工作流 { #operate-the-complete-workflow }

| 任务 | 入口 | 使用前确认 |
| --- | --- | --- |
| 复用专业知识 | `raven skill list` | 正文、所需工具、下载同意策略 |
| 保留知识 | `raven plugins` 与记忆配置 | 后端健康和预期 user/agent 身份 |
| 连接工具服务 | MCP 配置、`raven plugin auth --help` | 凭据范围与实际工具可用性 |
| 协调 Agent | DAG 或 playbook | Roster 就绪、引用和共享上限 |
| 定时执行 | `raven cron --help` | 计划、接收方、权限和常驻宿主 |
| 启用主动行为 | [主动提醒与跟进](proactivity.md) | 显式启用、打扰策略、成本 |
| 连接消息渠道 | `raven channels --help` | 发送者白名单、凭据、投递 |
| 诊断运行 | `raven doctor`、tracing、trajectory 工具 | 脱敏证据，而不只是握手成功 |

周期性或无人值守运行前，先读[权限与安全](permissions.md)和[自托管](self-hosting.md)。
模型上下文、记忆、下载的技能、子进程和远程 peer 各有自己的信任与成本边界。
