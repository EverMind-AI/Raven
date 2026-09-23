# 使用 Oncall 托管长任务

模型训练、求解器计算、参数扫描等工作，往往需要在提交后多次查看状态，
决定继续运行还是调整执行，并在结束时汇总有实测证据的结果。

本指南说明如何使用 Raven-Oncall，在声明的预算和允许操作范围内托管这类任务：
准备机器、委派任务、跟进进度、按需停止，并验收结果。
先按下文示例测试一个有明确边界的本地作业，再托管更长时间的任务。

**初次委派在第一个轮次后返回，不会等到长任务结束。**因此，Oncall 的 DAG 节点
返回不证明远程产物或最终报告已就绪。

## 适用场景 { #when-to-use-oncall }

当任务需要多轮观察或调整时，Raven-Oncall 可以在声明的预算和允许操作范围内
执行任务、检查输出、决定下一步，并汇总实测结果。

**Campaign** 记录目标、机器、执行配置、baseline、预算、trial 与后续状态。
Agent 的调度器可以安排下次观察，但计划中的观察不是作业本身。

## 准备 Agent 与机器 { #prepare-the-agent-and-machine }

按[Agent 集成](agent-integrations.md)检查 Raven-Oncall，包括模型凭据。
后续工作需要宿主、Agent 运行时和调度器保持可用。关闭浏览器不等于停止常驻运行时。

宿主管理机器注册表：

```bash
raven ops connection list
raven ops connection add
raven ops connection doctor
```

`add` 默认交互式执行，并联系选定的本地/SSH 机器，记录身份、访问参数、软件、
工作路径、预算单位与并发数。替换解释器和项目路径后，可用以下非交互本机示例：

```bash
raven ops connection add --id local-lab --name "Local lab" \
  --transport local --software "Python at /absolute/path/to/python" \
  --budget-unit minute --concurrency 1 --path /absolute/path/to/project \
  --non-interactive
```

注册会写入 raven 家目录（`RAVEN_HOME`，默认 `~/.raven`）下的 `connections.json`，
或 `RAVEN_CONNECTIONS` 指定位置；家目录里没有时，仍会读放在别处 config 旁的那份。
子代理各自用渲染出的 config 跑在自己的状态目录里、继承家目录，所以读到的是
主人的注册表，不是一份副本。SSH 注册还可能在 `~/.ssh/config` 写入受管理的别名；
只记录私钥路径，不记录私钥内容。不要把密钥或密码放进任务消息。

agent 也能登记机器。`ops_connection_add` 工具（raven-oncall 与 raven-code 通过
`tools.connectionAdd` 开启，其他产品关闭）接主人在对话里说的话——是这台电脑还是
另一台、叫什么、另一台的话地址是什么——用主人自己的 ssh 配置补上没说的端口、
用户名或密钥路径，先连上机器，连上了才写入。注册表里没有合适的机器时，agent
会问主人，而不是拿一条原始 ssh 命令自己找路进去。

`doctor` 校验注册表可用性，不会重新执行每台远程环境的连通测试。
`add --skip-probe` 不联系机器，并明确保留“未验证连接”状态。
如何在机器上执行 campaign 由 Oncall Agent 决定，不是宿主 DAG 注册表决定。

## 提供有边界的任务说明 { #give-a-bounded-work-order }

先用可丢弃的本地作业测试，并提供：

| 字段 | 示例 |
| --- | --- |
| 机器与目录 | 注册的 “Local lab”，临时项目目录 |
| 目标 | 运行既有 benchmark，报告是否达到目标 |
| 命令与环境 | 确切的已有脚本、解释器和输入 |
| Baseline 与指标 | 之前实测值、单位、越高还是越低越好 |
| 资源预算 | 总机器分钟数与并发上限 |
| 允许动作 | 读取输出，仅重试列出的参数变更 |
| 停止/升级条件 | 预算用尽停止；改依赖前询问 |
| 交付 | 实测证据、输出位置及未解决问题 |

示例请求：

> 委派给 Raven-Oncall，在注册的 Local lab 机器上运行。使用临时项目和我附带的既有
> smoke 脚本，提交前先检查并声明执行配置。最多十个机器分钟，同时一个作业。
> 不安装软件包、不修改 baseline。持续检查结果，预算耗尽时停止，并报告实测结果和日志。

这是需要翻译为 campaign 的用户要求，不是 CLI 参数。
检查实际声明：预算只出现在文字里，不证明已配置的计量器会执行它。

## Agent 如何工作 { #what-the-agent-does }

```text
声明目标/配置 -> 提交或观察 -> 检查状态与输出
    -> 安排下次观察 / 调整允许参数 / 询问用户
    -> 停止或完成 -> 交付证据
```

以下是 **Raven-Oncall 内部的模型工具**，不是宿主 Shell 子命令：

| 工具 | 职责 |
| --- | --- |
| `ops_declare` | 记录目标、配置、baseline 和预算 |
| `ops_submit` | 在已声明配置和检查下提交 trial |
| `ops_tune_status` / `ops_outputs` | 检查 campaign 状态及输出证据 |
| `ops_check_later` | 安排下次观察 |
| `ops_campaigns` / `ops_note` | 查找 campaign、记录决策 |
| `ops_ask_owner` | 评估是否请求人工介入 |
| `ops_kill` / `ops_finish` | 停止工作或提交报告并关闭 campaign |

下次观察以 campaign 为 key，新计划替换待执行计划，而不是无限累加轮询。
没有调度器或可用路由时，工具会提示需要手动检查；不能将此提示当成无人值守观察已启动。

## 预算与审批边界 { #budgets-and-approval-boundaries }

Campaign 可计量 compute、观察的墙钟时间或观察次数。单位和并发很重要：
十个 GPU-minute 不一定是十分钟经过时间。未声明预算的 campaign 没有总预算停止保护。
这些计量器不会限制模型账单，也不覆盖所有外部云费用。

Oncall 的声明、执行检查和升级策略，与宿主[工具权限](permissions.md)不同。
特别是，`ops_ask_owner` 允许后返回供模型通过消息工具发送的文字，不证明用户已收到；
通用消息发送仍是独立工具。

随 Raven 发布的 Oncall 配置默认未启用沙箱，也不会把所有执行限制在项目目录内。
允许无人值守修改前，使用限定账号、临时路径和隔离环境。
宿主批准不代表每条子 Agent 命令都另经人工审核。

## 跟进、停止与确认结果 { #follow-up-stop-and-verify }

通过[实例协作](agent-collaboration.md)向同一个 Agent 查询 campaign 状态、
待执行观察和最新输出证据。要求停止时明确 campaign，要求检查运行作业、
取消后续观察，并报告实际停止了什么。

关闭 UI、取消宿主轮次或忘记实例，不等于 kill 远程作业。
运行时中断后，重新提交前检查持久化 ledger、远程进程和调度路由。
保存了 campaign 文件，不保证每种传输都会自动恢复观察。

最终报告应包含目标、必要的 baseline、实测值、终止原因和可获取的输出位置。
没有证据的“完成”消息不是 benchmark 结果。

## 排障 { #troubleshooting }

| 现象 | 检查 |
| --- | --- |
| Agent 无法选定机器 | 注册 ID/名称、访问字段和软件路径 |
| 提交被拒绝 | 已声明配置、可用资源、预算和固定 baseline 检查 |
| 第一条回复到了但作业仍在跑 | 符合首次轮次行为；查看 campaign |
| 没有后续更新 | 常驻运行时、调度器、wake 路由和投递渠道 |
| 预算与经过时间不一致 | 计量器、单位、并发和已记录消耗 |
| 重启后状态不确定 | 重试或停止前读取 ledger 与远程状态 |

实现依据位于 `agents/raven-oncall/plugins/oncall-flow/`、
`raven/ops/connections.py`、`raven/ops/connection_add.py`、
`raven/agent/tools/connection_add.py` 和 `raven/cli/ops_connection_commands.py`。
