# Playbooks

Playbook 用来保存一类任务的可复用编排。需要审核、共享并用不同输入重复运行同一
工作流时，可以使用它。它保存一张图，或组装图的指导；执行仍使用 Raven 原有的
DAG 机制。

## 选择合适的能力 { #choose-the-right-capability }

| 能力 | 适用场景 | 不提供什么 |
| --- | --- | --- |
| Skill | 可复用的指令、脚本和参考资料 | 保存好的多 Agent 执行图 |
| `run_subagent_dag` | 为当前任务临时组图 | 可复用的命名库条目 |
| Playbook | 可重复的审查、研究或其他多步骤流程 | 调度计划或持久工作流服务 |
| Evolver | 用基准评估 harness 修改的实验 | 执行用户日常工作流 |

例如，发布审查 Playbook 可以并行检查兼容性和测试，再汇总结果；事故复盘
Playbook 可以先收集证据，再起草报告。应从只读工作流开始，发送消息或修改
基础设施需要另外考虑权限。

调度和失败处理见[DAG 编排](orchestration.md)，整体能力划分见
[技能、记忆与扩展](skills-and-extensions.md)。

## 检查与创建 { #inspect-and-create }

```bash
raven playbook list
raven playbook create brief-review \
  --input "Review a supplied brief for gaps, then summarize actionable feedback. Do not edit files."
raven playbook get brief-review
raven playbook validate brief-review
```

`create` 使用已配置模型；`--from FILE` 可代替或配合 `--input` 提供工作流材料。
运行前检查生成的参数和步骤：生成结果不保证与下方示例完全一致。
`get` 将文件输出到 stdout，将来源路径输出到 stderr。

**新建 Playbook 会立即可用**，尽管旧 CLI help 仍描述为创建后禁用。
审核期间可用 `raven playbook disable brief-review` 暂停提供给模型。
如果需要先审核、后发现，应在库目录之外编写，按路径校验，准备好后再安装。

库分为 `raven/playbook/builtin/` 下的只读打包层和可写用户层。
打包层可能没有任何条目。用户层默认是 `config.workspace_path / "playbooks"`
（配置的 Agent home）；`playbooks.dir` 可覆盖此路径。
同名用户文件会遮蔽内置文件。库文件变化和禁用列表会实时读取。

## 完整的 DAG Playbook { #a-complete-dag-playbook }

将以下内容保存为编写目录下的 `brief-review/playbook.md`。如果已经生成了同名
条目，应审核并编辑原文件，不要再创建第二份定义。

````markdown
---
name: brief-review
description: Review a brief for gaps and summarize actionable feedback
---

# Brief review

Inspect the supplied brief, then produce a prioritized checklist.

```yaml playbook-spec
version: 1
taskSummary: Review a brief
mode: dag
confirm: true
triggers:
  keywords: [brief review, review brief]
params:
  brief:
    type: string
    required: true
    description: The brief to inspect
nodes:
  - id: inspect
    subagent: raven
    nodeSummary: Identify gaps
    skills: []
    mcps: []
    promptTemplate: "Find gaps in this brief: {{ params.brief }}. Do not edit files or contact external services."
  - id: summarize
    subagent: raven
    nodeSummary: Prioritize feedback
    dependsOn: [inspect]
    skills: []
    mcps: []
    promptTemplate: "Turn these findings into a prioritized checklist: {{ inspect.output }}. Do not edit files or contact external services."
```
````

`raven` 是通用内置 Agent。如需 Raven-Code 等专用 Agent，替换为已注册的名称，
并按[Agent 集成](agent-integrations.md)检查配置。保存的 Playbook 使用底层
Agent registry，而不是临时组图时向模型展示的 worker label。

```bash
raven playbook validate ./brief-review/playbook.md
```

审核后将目录放入用户库根目录，再运行：

```bash
raven playbook run brief-review 'brief=Launch a read-only documentation preview for internal reviewers.'
```

示例中的指令和空 Skill/MCP 选择不是只读沙箱。普通工具仍遵循 Agent 的权限策略。

文件由三部分组成：只有 `name` 和 `description` 的 frontmatter、面向人的正文、
以及恰好一个 `yaml playbook-spec` 区块。名称与目录名一致，使用小写字母、数字
和连字符。`taskSummary` 是调度任务的标题，与用于发现的 description 不同。
未知契约字段会报错。

## DAG 模式与 prompt 模式 { #dag-mode-and-prompt-mode }

`mode: dag` 要求非空 `nodes`，不能包含 `prompts`。保存的图经过填充后由
`SubAgentDagTool` 调度。独立节点可以并行；引用其他节点时需要声明依赖。

`mode: prompt` 要求 `prompts`，不包含图。例如，将示例的 `mode` 和 `nodes`
字段替换为以下片段：

```yaml
mode: prompt
prompts: |
  Build a read-only review graph for this brief: {{ params.brief }}.
  Use registered agents to inspect clarity and feasibility independently.
  Add a final synthesis node depending on both inspections.
  Do not edit files or contact external services.
```

对话中，`load_playbook` 返回已填充的指导，由调用方组图并提交
`run_subagent_dag`。CLI 则使用已配置模型组图，允许有限次数的修复尝试。
两条路径都使用普通 DAG 契约。Prompt 模式是组图指导，不是运行时分支语言；
应检查实际提交图的选项，包括审批设置。

## 参数与空字段 { #parameters-and-blank-fields }

在 `params` 中声明输入，支持 `string`、`integer`、`number`、`boolean`、
`enum`、`path` 和 `secret`。普通参数引用直接写在 `promptTemplate` 或
`prompts` 中，不要写入节点 `inputs`。支持 `{{ params.brief }}` 和 dollar-brace
两种参数引用形式。

当前 resolver 进行文本替换并检测缺少的必填值；仅声明类型，并不意味着运行时
会强制检查数字、枚举成员或路径访问限制。敏感输入应在消费它的工具中校验，
文件系统和工具权限要独立落实。

`fills` 只能补全作者留空的字段。必需字段包括 `subagent`、`nodeSummary` 和
`promptTemplate`；可选的可填字段包括 `skills`、`mcps` 和 `instance`。
不能修改已有值。显式空列表是固定选择，不是留空。

如果某个变体刻意将 `inspect.promptTemplate` 留空，可以使用：

```bash
raven playbook run brief-review 'brief=Review the launch plan.' \
  --fill 'inspect.promptTemplate=List unclear assumptions in the launch plan; do not edit files.'
```

对前面的完整示例执行此命令会被拒绝，因为它已写明 prompt。CLI `validate`
检查完整性，因此会报告必需字段留空；运行时仍可加载此类模板并请求 fills。
CLI `--fill` 用于字符串字段；列表形式的 Skill/MCP fills 应通过结构化工具参数提供。

## Agent、Skill 与随附 MCP 服务 { #agents-skills-and-carried-mcp-servers }

节点字段遵循[DAG 契约](orchestration.md#node-contract-and-data-flow)。
省略 `skills` 或 `mcps` 保留该 Agent 的菜单或默认选择；`[]` 请求不附加任何项。
非空列表请求指定选择，具体取决于后端支持。共享 `instance` 要求有状态 Agent，
且节点以依赖链顺序使用同一个 Agent。并行不会自动提供独立工作副本。

DAG Playbook 可以携带使用宿主 MCP 配置 schema 的 `mcpServers` 定义。
本次运行中，它的定义优先于宿主同名服务。以下片段可加入 DAG Playbook；
替换示例 endpoint，并在消费节点添加 `mcps: [docs-api]`：

```yaml
params:
  API_TOKEN:
    type: secret
    description: Credential for the documentation service
mcpServers:
  docs-api:
    url: https://mcp.example.invalid/mcp
    headers:
      Authorization: "Bearer {{ params.API_TOKEN }}"
```

与现有参数合并；这不是完整 Playbook。随附命令、URL 和工具应按可执行配置审核。
加载器会丢弃无法使用的服务定义并记录警告。Prompt 模式携带的定义也会被
丢弃，因为后续由调用方组装的图无法接收它们；该模式应使用宿主配置的 MCP。

缺少服务、凭据或后端不支持注入时，运行可能降级而不是整体停止。依赖某项工具的
回答不能仅凭文字相信，应先检查能力提示并确认工具确实到达 Agent。

## 在本地保存凭据 { #store-credentials-locally }

针对上方片段，可用以下命令保存 secret，避免写入 shell history：

```bash
raven playbook secret set brief-review API_TOKEN
```

省略 `--value` 时会无回显地提示输入。文件以 `0600` 权限保存在
`<credentials>/playbooks/brief-review/params.json`；这是本地文件，不是加密保险库。
参数必须声明为 `type: secret`，且不能有默认值。只有随附 MCP 的 `env`/`headers`
引用会获得其值；prompt 中的 secret 引用会被隐藏并记录日志。

不要通过聊天、普通参数或 CLI `K=V` 提供 secret。这里保护的是参数替换路径，
不是外部工具所有可能的回复；接收凭据的服务仍处于信任范围内。

对于声明了 `auth: oauth` 的随附服务：

```bash
raven playbook auth brief-review docs-api
```

该命令需要 OAuth 服务定义，不适用于上方 bearer-header 示例。
OAuth token 位于 `<credentials>/playbooks/brief-review/mcp/`，与宿主服务凭据隔离。
使用 `raven playbook secret clear brief-review API_TOKEN` 删除已保存参数。

## 发现、审批与执行 { #discovery-approval-and-execution }

库较大时，`triggers.keywords` 影响向模型展示哪些完整描述。关键词永远不会自动
执行 Playbook。模型显式调用 `load_playbook`；即使检索没有选中完整描述，
启用的名称仍然可达。

CLI `run` 同步等待；对话中的 DAG 加载通常后台调度。缺少普通参数或必需字段
会阻止调度并返回命名缺口。Prompt 模式加载只返回指导，本身不调度。

Playbook 的 `confirm` 默认 true，executor 调度时会将它传给图。
**没有 ask channel 时，当前 DAG 实现记录提示后以未确认状态运行；
显式 CLI 路径没有接入 ask channel。**有 ask channel 时，拒绝或投递失败会
阻止调度。这不是无人值守安全门，详见[权限与安全](permissions.md)。

```bash
raven playbook disable brief-review
raven playbook enable brief-review
```

Disable 将条目从模型可见库中移除，但显式 CLI `run` 仍可执行它。
`raven playbook delete brief-review` 会询问确认后删除用户条目的目录，
Playbook 没有撤销删除功能。删除覆盖副本会重新露出同名内置条目并清除其禁用状态；
内置条目本身只能禁用。

## 排障与安全运行 { #troubleshooting-and-operating-safely }

| 现象 | 检查 |
| --- | --- |
| 模型工具中看不到条目 | 禁用列表、解析错误、已注册 Agent，以及当前 Agent 是否有 Playbook 工具 |
| 校验提示 Agent 未注册 | 使用当前底层 roster；注册不等于已安装或就绪 |
| 运行提示缺少值 | 补充普通 `params` 和必需空字段；secret 在本地保存 |
| Fill 被拒绝 | 字段已有固定值、节点 ID 错误，或字段不允许填充 |
| 缺少 MCP 工具 | 宿主/随附定义、凭据、连接状态和后端注入支持 |
| 运行返回但工作未完成 | 检查节点结果和 verdict；收到回执不证明任务成功 |
| 图执行时宿主重启 | 检查已有输出和副作用，再把剩余工作作为新图提交 |

安装前校验并审核文件，使用限定凭据测试，在允许修改前检查一个小型只读运行。
版本控制保存工作流定义，不保存凭据或 transcript。DAG 历史恢复不自动恢复执行，
再次运行可能重复外部副作用。
