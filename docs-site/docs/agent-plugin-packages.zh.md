# Agent 插件包

Raven 支持读取 [Agent Plugins 1.0](https://agent-plugins.org/specification)
的可移植包格式：一个目录，根目录下有 `plugin.json`，可选带 `skills/` 与
`mcp.json`。多个生态用这个格式发布包——例如 `agentic-awesome-skills`
仓库就发布了三十个。

这和 Raven 插件**不是一回事**。Raven 插件（`raven-plugin.toml`）携带在宿主
进程内运行的 Python 工厂；Agent 插件包是纯数据：技能，以及后续阶段才会用到的
MCP server。包里的任何东西都不会被当作代码 import。

## 一个包长什么样

```text
my-package/
├── plugin.json          # 必需，位于包根目录
└── skills/
    └── my-skill/
        ├── SKILL.md     # 每个技能目录都必需
        ├── scripts/     # 可选
        └── references/  # 可选
```

`plugin.json` 会按官方 Agent Plugins manifest schema 校验。声明了 Raven 尚未
实现的 Agent Plugins 版本的包会被拒绝，破坏 schema 的 manifest 也会被拒绝；
未知字段只会被报告并忽略。

## 让 Raven 找到包

```json
{
  "agentPlugins": {
    "packages": ["/opt/agent-plugins"]
  }
}
```

每一项要么是包根目录（含 `plugin.json` 的目录），要么是其直接子目录都是包根
目录的目录。相对路径以工作目录为基准解析。`agentPlugins.disabled` 列出要跳过
的包。

## 命令

```bash
raven agent-plugins list                 # Raven 能看到的包
raven agent-plugins inspect /opt/my-package   # 只校验，不导入
raven agent-plugins import /opt/my-package    # 导入其中的技能
raven agent-plugins check                 # 报告已过期的副本
raven agent-plugins remove my-package     # 删除导入的技能
```

每条命令都支持 `--json` 以便脚本调用。

## 导入做了什么

包的技能会被复制到工作区技能树下的 `<workspace>/skills/<包名>/`。一个包一个
目录，意味着技能池按包生成一个来源标签，而不是按技能生成——这样两个包各自
带一份 `postgres-best-practices` 时，谁也不会遮蔽谁。

这份副本记录在包账本里（`<home>/agent-plugins.json`）。正是这份记录让
`remove` 变得安全：只有账本认领的目录才会被删除。`check` 会拿账本里的指纹
与磁盘上的包比对，导入之后包更新过就会报告为 stale。

导入之后这些技能就是普通的工作区技能：文件监视器会看到它们，路由可以检索
它们，技能里的脚本仍然通过普通的 `exec` 工具、在普通的工作区围栏内运行。

## 包做不到什么

- **不能把自己注入每一轮对话。** 导入的副本会被去掉 `always` 标记。包的技能
  仍然可被检索，也就是在相关时由路由取用。
- **不能绕过操作者的屏蔽名单。** 名字在 `skillForge.blocklist` 上的技能不会被
  导入；正文里指向别的产品家目录（`~/.openclaw` 之类）的技能会被拒绝。
- **不能在导入时执行代码。** Raven 只复制文件；技能里的脚本只有在模型决定运行
  它们时才会通过 `exec` 执行。
- **不能覆盖你自己的技能。** 存在且不属于账本的目标目录会被原样保留，导入会
  如实报告。

## 当前限制

- 第一阶段只读技能。包里的 `mcp.json` 还不会启动；启动它需要按包显式授权
  （`agentPlugins.mcpPackages`），因为那是一个需要你同意运行进程。
- Raven 不下载包。包是你已经拥有的目录；没有目录客户端，也没有更新通道。
- 其他工具的宿主原生格式（`.claude-plugin/`、`.codex-plugin/`）暂时还不读取。
