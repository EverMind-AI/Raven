# 权限与安全

权限、认证、隔离分别回答：**动作可否执行**、**调用者是谁**、**进程能访问什么**。
三者都需要配置。已认证的 A2A 请求和沙箱都不能替代 Permission Gate（权限门）。

## 决策顺序 { #decision-order }

工具注册表在分发调用前检查权限门：

1. 内置规则拒绝高破坏性命令，例如递归删除根目录或用户主目录。
2. `permissions.tools` 中的用户规则选择 `deny`、`allow` 或 `ask`。
3. 未命中规则的调用采用工具的默认权限等级。
4. 对于 `ask` 等级，由当前权限模式决定如何处理。

只读工具和已识别的只读本地命令通常默认 allow；修改操作和未知 MCP 工具通常默认 ask。
也有显式默认允许的例外，例如把文件交给请求用户的 `deliver_files`。用户规则仍优先于
默认值。不要因为工具未出现在配置中，就认为它已被禁止。

## 模式 { #modes }

| `permissions.mode` | ask-tier 调用的处理 |
| --- | --- |
| `ask` | 询问交互式用户 |
| `smart`（默认） | 模型审查后允许，或升级给用户 |
| `full` | 不询问，直接执行 |

所有模式都遵守内置拒绝规则和用户配置的 `deny`。Smart 模型审查不能替代明确的禁止规则。
审查失败或超时会转交用户确认，不会自动批准。没有可用的审批响应端时，需要确认的调用会被拒绝。
`raven agent -m` 始终没有审批响应端：回复之后会列出所有被拒绝的调用；只要其中有需要确认的调用，
进程以状态码 3 退出，便于无人值守的调用方区分"跳过了修改"与"完成了修改"。单次任务确需修改时，
请传入 `--permission-mode full`。

全局配置决定起始模式；对话可通过会话级 `config.set` 覆盖，并把模式保存在对话记录中。
排障时应同时检查对话模式与全局值。

## 配置工具与命令前缀 { #configure-tools-and-shell-prefixes }

可将以下片段合并进已有配置：

```json
{
  "permissions": {
    "mode": "ask",
    "tools": {
      "exec": {
        "*": "ask",
        "git status *": "allow",
        "git diff *": "allow",
        "git push *": "deny"
      },
      "write_file": "ask",
      "edit_file": "ask",
      "a2a_send": "ask"
    }
  }
}
```

只有 `exec` 使用命令前缀表，其他项按工具名配置权限等级；不能用它配置任意文件路径的访问控制。
示例禁止普通的直接 `git push`，并不能阻止 Shell 程序发布数据的所有可能方式。

Shell 匹配遵循以下规则：

- 具体模式按 token 前缀匹配（`git status *` 也匹配 `git status`）。多个**具体**模式
  命中时取最严格值：`deny > ask > allow`，与顺序无关。
- 只有没有具体模式适用时才使用 `*`。因此 `* = ask` 不会阻止具体的
  `git status * = allow` 生效。
- 复合命令只有每个 segment 都允许才允许；任一 segment 拒绝会拒绝整个调用。
  `git *` 不会隐式授权 `sudo git ...`。
- 命令/进程替换、反引号、heredoc 或越界重定向可能导致只使用 fallback 规则。
  这是保守解析，不能证明命令启动的每个程序都安全。

与 OpenCode 的有序规则不同，Raven **不采用最后匹配优先**。不要直接粘贴另一产品的
权限 schema 或优先级。除非明确需要宽泛的无人值守执行，否则避免 `"*": "allow"`。

## 审批范围 { #approval-scope }

| 选择 | 范围 |
| --- | --- |
| 允许一次 | 当前等待的动作 |
| 本会话允许 | 该对话中仍需询问的动作键 |
| 不再询问 | 用户确认且通过校验的 `exec` 前缀，写入配置 |
| 拒绝 | 拒绝动作，通常轮次仍可继续 |
| 拒绝并停止 | 拒绝并结束当前轮次 |

Shell 会话授权包含命令 segment、机器和工作目录；内置文件写工具按路径和工作目录
记忆；浏览器操作工具共享站点级授权键。其他工具通常按精确调用记忆。会话授权保存在
内存中，不等于持久化全局前缀规则；两者都不能覆盖 deny。过期或被拒绝的动作在同一
轮次内不会原样再次询问。

## 委派是独立信任边界 { #delegation-is-a-separate-trust-boundary }

Raven 的权限门管理自身注册表调用，不是外部 Agent 进程内每一条命令。

- **入站 ACP：** Raven 可通过 `session/request_permission` 请求编辑器或宿主审批。
- **出站 ACP：** Raven 无人值守客户端自动优先选择子 Agent 的 `allow_always` 或
  `allow_once`；启用 Agent 不代表每个内部操作都有人审阅。拒绝规则会传过去：请求中的
  shell 命令命中你的 `deny` 规则或 `tools.exec.extraDenyPatterns` 时，Raven 选择子
  Agent 的拒绝选项；Raven 自家 Agent（Raven-Code 等）的渲染配置也带上这些规则，不经
  询问即拒绝。第三方 Agent 只在它发出请求的调用上受约束；新增的规则要等正在运行的
  Raven Agent 下次启动才生效。ask 档和权限模式不会传递。
- **CLI 和其他后端：** 检查启动命令、环境、工作目录及原生权限策略。宿主规则不会
  自动跨所有后端继承。
- **A2A：** 共享 bearer token 接纳远程操作者。宿主工具门仍有效，但当前入站 A2A
  轮次没有工具审批 responder；问题 broker 不是权限 UI。
- **DAG：** `confirm: true` 请求用户确认已声明的任务图，不会逐一审批运行期间新增的动作。
  未接入确认通道时，当前实现会记录提示并继续执行，因此不能用这个开关保证“未审批就不执行”。
  技能/MCP 选择依赖后端能力，降级通知意味着不能在那里把它当作可靠执行边界。

允许无人值守子 Agent 时，优先使用窄范围凭据、隔离账户或容器，并明确划分工作文件。

## 沙箱、文件与不可信内容 { #sandbox-files-and-untrusted-content }

沙箱控制执行位置。BoxLite VM 可读写挂载真实工作区，在挂载中删除就会删除真实文件。
所以权限门不会仅因存在沙箱就自动允许命令。第三方后端可能不使用 Raven 的执行器。
详见[沙箱](sandbox.md)。

工作目录保护、DAG 引用边界、渠道发送者白名单和网络限制是作用范围不同的附加控制。
Prompt injection 标签把工具结果和子 Agent 输出标为不可信数据，但不能使模型判断
永不出错。凭据与网络可达范围应独立于 prompt 指令检查。

ACP 会脱敏出站参数、预览、错误和权限描述中可识别的秘密。A2A 轮次失败只暴露固定
消息，详细异常留在本地日志。两者都不保证每份日志和 transcript 可安全公开。尽量
不要把秘密放进任务 prompt，分享诊断导出前应审阅并脱敏。

## 排查拒绝或停滞的动作 { #diagnose-a-refused-or-stalled-action }

| 现象 | 检查 |
| --- | --- |
| `full` 仍拒绝命令 | 内置或用户 deny，切换模式不会覆盖它 |
| Smart 意外询问 | 审查升级、超时或服务商不可用 |
| 无界面任务拒绝修改 | 没有审批 responder，启动前应配置必要的窄权限 |
| 复合命令要求确认 | 每个 segment 和重定向都需要覆盖 |
| 之前的授权不适用 | 目录、机器、站点、对话变化，或进程已重启 |
| 子 Agent 未询问就执行 | 检查出站 ACP/子 Agent 原生策略，不只是宿主权限门 |

可检查工具调用 trace span 上的 `permission.decision`、`permission.source` 和 smart
reviewer 字段。实现入口为 `raven/permissions/gate.py`、`raven/permissions/rules.py`、
`raven/permissions/session.py` 和 `raven/acp_client/permissions.py`。
