# 与子 Agent 协作

委派范围明确的任务，检查专用 Agent 实际完成的工作，并在需要修改时继续正确的对话。
Raven 同时提供委派任务与有状态 Agent 实例，两者不是一回事。

先按[Agent 集成](agent-integrations.md)完成配置。本页介绍如何协作，不介绍传输安装。

## 选择协作形式 { #choose-a-collaboration-shape }

| 需求 | 方式 | 完成证据 |
| --- | --- | --- |
| 单个专用任务 | 请宿主通过 `spawn` 委派 | 返回结果及相关文件/测试 |
| 并行或有依赖的工作 | [DAG 编排](orchestration.md) | 节点输出、依赖和 verdict |
| 重复已审核流程 | [Playbook](playbooks.md) | 实际图结果，而不只是调度回执 |
| 继续与特定专用 Agent 协作 | 与其有状态实例直聊 | 该实例的回复/历史 |
| 执行并持续观察长任务 | [Oncall](oncall.md) | Campaign 状态与最终报告 |

`spawn` 和 `run_subagent_dag` 是模型工具，不是 Shell 命令。委派会增加模型调用，
也可能启动子进程或外部工作。

## 第一次只读协作 { #a-first-read-only-collaboration }

1. 在项目工作目录中打开对话，检查目标 Agent 已启用且就绪。
2. 请求：“让 Raven-Code 检查这个仓库的公开 API，不修改文件，返回带文件引用的
   兼容性风险，再为我汇总。”
3. 在 WebUI 子 Agent 面板或任务视图检查委派记录和结果。活动流只是进展，不是成功证明。
4. 如果产生了可恢复实例，打开它并追问：“展开最高优先级风险，暂时不要修改实现。”
5. 返回主对话，请主 Agent 把追问结果纳入最终总结。

没有准备好专用配置时，也可以使用通用 `raven` Agent。需要后续对话时选择有状态后端；
一次性的无状态任务不会自动变成可恢复聊天。

## 会话、实例与任务 ID { #sessions-instances-and-task-ids }

宿主 session 是你打开的主对话。实例由该 session 内的 Agent 和 handle 共同定位。
DAG 节点 ID 标识任务与输出，不是可以直聊的会话地址。

复用实例可以保留子 Agent 上下文，新建实例则开启独立状态。WebUI 可为启用且有状态的
Agent 创建实例，不必先由宿主委派。创建只记录 idle 实例，本身不会执行任务。

不要假设所有 Agent 都看到宿主的完整 transcript。应传入所需的具体要求和附件。
路径指向接收它的执行环境，不会自动把文件传到所有远程 Agent。

## 直聊与交接 { #direct-chat-and-handoff }

子 Agent 面板展示实例历史，并在支持时提供面向该实例的输入框。直聊使用独立执行通道，
可以与宿主或其他实例并行，但忙碌的实例不一定能再接收普通轮次。

直聊记录与主 transcript 分开保存。下一次向主 Agent 发消息时，运行时提供
**Handoff Block**：实例身份、时间和已记录 prompt/result 的路径。
它不会粘贴整个交流，也不会自动生成语义总结。决策依赖这些内容时，应要求宿主读取相关记录。

宿主也会收到用户创建实例、直聊仍在运行的信息。这些是活动证据，不是完成证明。
忘记实例会移除它的访问地址，但不会删除用于审计的记录目录。

## 引导正在执行的轮次 { #steer-an-active-turn }

Steer 向正在执行的轮次补充修正，而不是另起任务。例如：“只审查公开接口，不包含内部 helper。”
修正会在后续模型调用之前读取，不能撤销已经执行的动作。

服务端提供 `subagents.instance.steer`。客户端 UI 支持可能不同；当前 WebUI 实例
输入框发送普通直聊轮次，因此不能假定输入文字就会调用 steer。实现该 RPC 的客户端可发送：

```json
{
  "session_key": "HOST_SESSION_KEY",
  "agent": "Raven-Code",
  "handle": "INSTANCE_HANDLE",
  "text": "Limit the review to public interfaces; do not edit files."
}
```

这是 RPC 参数，不是 CLI 命令。Session、Agent 和 handle 应从当前实例列表取得，
不要凭空构造地址。

| 回复 | 含义 |
| --- | --- |
| `injected` | 已接收供当前轮次读取，不代表已按此行动 |
| `no_turn` | 没有活跃轮次；仍需要时发送普通追问 |
| `unsupported` | 本次运行的传输不支持中途引导 |

CLI-agent 和没有协商 Raven steering 扩展的 ACP Agent 不能假定支持此功能。
详见[Agent 协议](agent-protocols.md)。

## 模型、模式与共享文件 { #models-modes-and-shared-files }

实例公布模式或模型时，控制项使用该实例自己的菜单。模式覆盖可优先于宿主 session 的
tier，清除后恢复继承；清除模型覆盖则回到 Agent 自己的选择。两者都不会授予新文件或工具权限。

并行实例不等于独立工作副本。并发修改时，应明确文件分工或提供独立 worktree。
Raven-Code 会报告共享工作目录情况，但这不是文件锁。

## 排障 { #troubleshooting }

| 现象 | 检查 |
| --- | --- |
| 找不到 Agent | 启用的 roster、安装就绪情况和当前 session |
| 无法创建或恢复实例 | 后端是否有状态，Agent 是否启用 |
| 忙碌时直聊被拒绝 | 等待该实例；中途修正需使用支持 steer 的客户端 |
| 主 Agent 没有使用追问结果 | 发送新宿主轮次，并要求检查交接记录 |
| 重启后能看历史但没有运行活动 | 记录恢复不等于自动恢复执行 |
| 返回正常但结果错误 | 重试前核对文件、证据和 DAG verdict |

停止或重试前检查副作用。取消对话不证明远程进程或 campaign 已停止；
定时长任务应按[Oncall 生命周期](oncall.md)处理。
