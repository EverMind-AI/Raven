# 浏览器与人工协作

Raven 浏览器工具与 WebUI Browser 面板使用宿主进程中的同一个 Chromium。
你可以看到 Agent 操作的页面，自己完成登录，再让 Agent 从新状态继续。

这是浏览器自动化，不是无限制桌面控制。桌面应用需要独立 MCP 集成及操作系统权限。

## 启用浏览器 { #enable-the-browser }

在源码树中执行：

```bash
uv sync --extra browser
uv run --extra browser playwright install chromium
```

Linux 还可能需要 Chromium 系统库：

```bash
uv run --extra browser playwright install --with-deps chromium
```

最后一条命令可能安装系统依赖，请先确认适用于当前机器。托管安装使用自己的环境；
缺少 binary 时，使用 Raven 为对应解释器提示的命令。运行 `raven doctor`，
再打开[WebUI](webui.md)。仅安装 Python 包不等于已安装可用浏览器。

浏览器在使用时惰性启动。如需 Agent 首次导航时在宿主桌面弹出原生窗口，合并：

```json
{
  "tools": {
    "browser": {
      "headfulOnAgentUse": true
    }
  }
}
```

默认 false，需要运行 Raven 的机器有图形桌面；不会在另一台远程客户端上弹窗。
修改后需重启进程。弹出窗口会保留浏览器 profile；关闭标签页不等于清除登录 cookie。

## 尝试只读任务 { #try-a-read-only-task }

1. 在与对话相同的宿主中打开 Browser 面板。
2. 请求：“打开我提供的文档 URL，总结安装前置条件。不提交表单、不下载文件、不改设置。”
3. 观察选中的标签页，对照实际页面检查回答。
4. 如果继续前页面发生变化，请求截图或重新读取 snapshot。

`browser_snapshot` 返回可见文本和可操作元素 ref。每次读取都可能重新编号，
不能把旧 snapshot 的 ref 当成稳定 selector。

## 把敏感步骤交给人 { #hand-a-sensitive-step-to-the-human }

遇到登录、CAPTCHA、支付或意外授权页时：

1. 要求 Agent 先停下并询问你。
2. 自己在共享浏览器中完成敏感步骤，不将密码、恢复码或 token 粘贴进对话。
3. 明确告知可以继续，以及剩余允许范围。
4. 要求下次动作前重新获取 snapshot。

用户交互会记录为 touch，后续 Agent 读回可提示你在上次动作后介入过。
这只是协调信息，不是自动排他锁，也不保证所有活跃 Agent 都已暂停。

## 标签页与多个 Agent { #tabs-and-multiple-agents }

Owner 是当前进程内子 Agent 运行，否则是主对话。它会取得未被占用的当前标签页，
或新建标签页。后续调用绑定该页，即使面板显示另一页也不改变地址。

Agent 的动作会将自己的页切到前台，单纯读取不会。其他 owner 的页标为 held，
该 Agent 不能切换到它或关闭它。绑定在空闲十分钟后或标签页关闭时失效。
用户面板交互不受模型 owner 绑定限制。

共享的是同一进程的浏览器，不是部署中的所有浏览器。外部 ACP/CLI 进程可能有
独立状态，不能保证外部 Claude Code 会话使用面板的 Chromium。

## 权限与网络边界 { #permissions-and-network-boundaries }

| 模型工具 | 默认策略 |
| --- | --- |
| 导航、snapshot、截图、滚动、标签页管理 | Allow |
| 点击、输入、按键 | Ask，会话授权按站点归组 |

检查实际[权限模式](permissions.md)：`full` 跳过普通 ask-tier 提示。
站点授权比单个按钮更宽；提示可能显示 ref 和站点，而不是元素的可读标签。
必须不可用的工具应通过 `tools.disabledTools` 或明确权限规则禁用。

导航接受 HTTP(S) 和空白页，拒绝危险 scheme 和字面 link-local 目标。
`RAVEN_BROWSER_BLOCK_PRIVATE=1` 增加私网/回环地址检查。
URL 检查不是网络防火墙，不能完整防御 DNS 解析、重定向和所有子资源访问；
敏感宿主应采用网络隔离。

页面和截图都是不可信内容，也可能包含发送给所配置模型的隐私数据。
持久 profile 可保存登录状态。应使用专用账号/profile，不在广泛可访问的 Raven
部署中操作敏感账户。

## 排障与限制 { #troubleshooting-and-limits }

| 现象 | 检查 |
| --- | --- |
| 没有浏览器工具 | Browser extra、禁用列表，以及实际服务轮次的进程/Agent |
| Chromium 无法启动 | Binary、Linux 系统库，以及 headful 所需显示环境 |
| Agent 读取了另一标签页 | Owner 绑定；面板当前页不是 Agent 地址 |
| Snapshot ref 失效 | 重读当前页面，使用新 ref |
| 动作前没有审批 | 实际模式、显式 allow 规则和既有站点授权 |
| Popup 未立即反映 | 脚本创建的 popup 可能在下一次读取时接管 |

仓库真实浏览器测试覆盖本地表单、标签页归属、截图和 loop/RPC 路径，
不证明所有网站的认证或反自动化机制都可用。Driver 位于 `raven/browser/`；
仓库 `docs/browser-and-desktop.md` 另行记录桌面集成及其验证边界。
