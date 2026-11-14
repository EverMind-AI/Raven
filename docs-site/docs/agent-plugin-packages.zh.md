# Agent 插件包

Raven 支持读取 Agent Plugins 1.0
的可移植包格式：一个目录，根目录下有 `plugin.json`，可选带 `skills/` 与
`mcp.json`。多个生态用这个格式发布包——例如 `agentic-awesome-skills`
仓库就发布了三十个。

这和 Raven 插件**不是一回事**。Raven 插件（`raven-plugin.toml`）携带在宿主
进程内运行的 Python 工厂；Agent 插件包是纯数据：技能，以及后续阶段才会用到的
MCP server。包里的任何东西都不会被当作代码 import。

## 一个包长什么样 { #what-a-package-looks-like }

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

## 让 Raven 找到包 { #pointing-raven-at-a-package }

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

## 命令 { #the-commands }

```bash
raven agent-plugins list                 # Raven 能看到的包
raven agent-plugins inspect /opt/my-package   # 只校验，不导入
raven agent-plugins import /opt/my-package    # 导入其中的技能
raven agent-plugins check                 # 报告已过期的副本
raven agent-plugins remove my-package     # 删除导入的技能
```

每条命令都支持 `--json` 以便脚本调用。

`remove` 在没有删除失败时返回 0，即使它保留了一个无法验证的条目——此时包目录
和账本记录仍然存在。判断包是否真的卸载干净，要看 `kept`（`--json` 里的
`kept_details`），而不是退出码。

## 导入做了什么 { #what-an-import-does }

包的技能会被复制到工作区技能树下的 `<workspace>/skills/<包名>/`。一个包一个
目录，意味着技能池按包生成一个来源标签，而不是按技能生成——这样两个包各自
带一份 `postgres-best-practices` 时，谁也不会遮蔽谁。

这份副本记录在包账本里（`<home>/agent-plugins.json`），每个工作区一份记录：
共用同一个 home 的两个工作区，各自维护对同一个包的导入，从其中一个卸载不会
动另一个。包根和每个技能根都带一个 `.raven-install.json` 标记，其随机
token 一并记录在账本里——正是这份记录让 `remove` 变得安全：只有标记仍然与
记录的 token 匹配的目录才会被删除。`check` 会拿账本里的指纹与磁盘上的包
比对，导入之后包更新过就会报告为 stale，同时报告已安装的副本是否仍是当初
导入的那份。

导入之后这些技能就是普通的工作区技能：文件监视器会看到它们，路由可以检索
它们，技能里的脚本仍然通过普通的 `exec` 工具、在普通的工作区围栏内运行。

## 什么时候 Raven 拒绝碰一个目录 { #when-raven-refuses-to-touch-a-directory }

一个目录存在于导入要写入的位置，但账本无法证明它是 Raven 装在那里的，
Raven 就不会写入它，也不会删除它。这包括：

- **你自己放在那里的目录。** 导入会拒绝整个包，并如实说明。
- **装在安装路径上的替代目录。** 把已安装的目录移走、在原路径放你自己的
  目录：记录不再匹配标记，`import` 和 `remove` 都会拒绝。把你的目录移开再
  重新导入，即可重新安装。
- **另一个工作区的导入。** 记录属于导入它的工作区；即使共用 home（也就是
  共用账本文件），第二个工作区的 `import` 和 `remove` 也碰不到它。
- **你在已安装包里替换掉的技能目录。** 原样保留；导入把它报告为 skipped
  或 released，`remove` 把它报告为 kept。包目录和账本记录会保留，把留下的
  条目移走后再执行一次 `remove` 即可。
- **立在安装路径上的符号链接或 junction。** 不管它后面是什么，Raven 都不
  会跟着走下去。

没有任何强制绕过的旗标：如果某个目录必须删掉，请自己移走它，再重新导入。

账本在记录安装身份之前（版本 1）写下的文件仍然可以读取。其中的记录会被
保留，并由 `check` 显示为 unverified，但它们无法再与磁盘上的任何目录匹配：
对这种记录执行 `remove` 什么都不会删，并如实说明。等目录让位之后重新导入
该包，会装出一份全新的、可验证的副本，并取代旧记录。版本 1 的记录不带工作区，
因此共用同一个 home 的任一工作区重新导入同名包，都会取代它。

如果账本文件本身读不了——版本不认识，或数据不合法——`import` 和 `remove`
会拒绝运行，而不是冒险覆盖它，文件保持原样。一次导入若没能保存账本，它的
目录会留在磁盘上但没有记录；在你把它们移开之前，重新导入会拒绝触碰它们。
一次中途失败的 `remove` 会同时保留账本记录和证明它的安装标记；等底层故障
解除后，再次执行同一命令即可完成卸载。

一旦账本写成了版本 2，就不要再让旧版 Raven 打开同一个 home。旧版读不了
新文件：它看不到任何记录，而下一次 import 会用版本 1 的文件覆盖账本，丢掉
所有工作区的记录。需要回滚时，请把账本和已安装目录一起恢复。

## 包做不到什么 { #what-a-package-cannot-do }

- **不能把自己注入每一轮会话。** 导入的副本会被去掉 `always` 标记。包的技能
  仍然可被检索，也就是在相关时由路由取用。
- **不能绕过操作者的屏蔽名单。** 名字在 `skillForge.blocklist` 上的技能不会被
  导入；正文里指向别的产品家目录（`~/.openclaw` 之类）的技能会被拒绝。
- **不能在导入时执行代码。** Raven 只复制文件；技能里的脚本只有在模型决定运行
  它们时才会通过 `exec` 执行。
- **不能覆盖你自己的技能。** 存在但不能证明属于账本——不属于这个工作区的
  记录，或不是其安装标记所指认的那份副本——的目标目录会被原样保留，导入会
  如实报告。

## 包里带的 MCP server { #mcp-servers-a-package-ships }

包可以在 `mcp.json` 里声明 MCP server。Raven 会读取它、逐条校验并报告结果；
在你点名某个包之前，它不会启动任何东西：

```json
{
  "agentPlugins": {
    "packages": ["/opt/agent-plugins"],
    "mcpPackages": ["my-package"]
  }
}
```

按包授权，是因为一条 server 配置就是要在你机器上启动一个进程。包贡献的
server 以 `<包名>__<server 名>` 命名，因此不会和你自己的 `tools.mcpServers`
条目冲突。

包的 server 以包根目录为工作目录（除非条目另指一个），环境里带
`PLUGIN_ROOT` 与 `PLUGIN_DATA`：前者是包目录，后者是 Raven 为该包保留、
跨更新不丢的目录（`<home>/plugin-data/<包名>/`）。格式定义的两个占位符
`${PLUGIN_ROOT}` 与 `${PLUGIN_DATA}` 会在 server 的参数、环境变量和工作目录
中展开。

展开之后，参数或环境变量里带 `..` 路径分量的条目会被拒绝并报告。该检查按
`/` 和 `\` 切分，所以 URL 里出现 `..` 分段（例如
`https://example.com/a/../b`）也会被拒绝。

一条坏配置不会让整个包失效：该条目被跳过并报告，其余照常运行。若包的
`mcp.json` 声明的 Agent Plugins 版本和它的 `plugin.json` 不一致，或文件本身
不是合法的 MCP 配置，则只关闭该包的 MCP，技能仍然可用。

## 当前限制 { #current-limits }

- Raven 不下载包。包是你已经拥有的目录；没有目录客户端，也没有更新通道。
- 其他工具的宿主原生格式（`.claude-plugin/`、`.codex-plugin/`）暂时还不读取。
- 包的 stdio server 以包根目录为工作目录，该目录必须位于挂载进沙箱的卷内。
  若其目录不在任何已挂载的卷里，该 server 会被拒绝，而不是在错误的位置启动；
  请挂载该目录，或不使用沙箱运行。
