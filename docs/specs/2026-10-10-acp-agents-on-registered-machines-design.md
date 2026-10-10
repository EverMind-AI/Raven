# 已登记机器上的 ACP 子 agent：设计

状态：G1 已签（2026-10-10，owner）；第 1 个 PR 在写
日期：2026-10-10
范围：后端。网页（`ui-web/`）不改。
关联文档：

- `2026-08-11-acp-local-agent-mesh-design.md`。它的非目标"远程 agent，只做 stdio 上的本地进程"由本稿修订，修订写在那份文档原处。
- `2026-09-03-exec-machine-permission-face.md`：机器登记表和 `exec(machine=)` 的先例。
- 摸底记录（不进仓库）：2026-10-10 在 ravenx、ravenx2 上的实测。下文凡写"已实测"的，都出自这次摸底。

## 术语

| 词 | 意思 |
|---|---|
| 机器 | `connections.json` 里的一行，按 `id` 引用。由 `raven ops connection add` 写入，`raven/ops/connections.py` 读取。_不要说_：主机、节点（节点是 DAG 的词）。 |
| 远端 agent 行 | `subagents.agents[]` 里一条 `kind: "acp"`、并且带 `machine` 的行。 |
| agent 命令 | 远端 agent 行的 `command`，指在那台机器上运行的命令，例如 `npx -y @agentclientprotocol/claude-agent-acp@0.81.1`。行里从不出现 ssh。 |
| 启动行 | Raven 在启动那一刻用登记表拼出的 `ssh ... -- <远端命令>`。只存在于内存，不写盘，不进日志正文，也不进任何模型或页面看得到的文本。 |
| 远端根目录 | 远端 agent 行在机器上的工作根目录：`remoteCwd`，默认 `~/raven-work`。 |
| 会话目录 | `<远端根目录>/<handle>`。`handle` 就是 `run()` 里已有的 `instance or task_id`。 |

## 目标

让一个 ACP 子 agent 跑在已登记的机器上。主 agent 用现有的 `spawn` 或 `run_subagent_dag`，就能把任务同时派给多台机器上的 agent。

会话、取消、权限请求、流式输出和结果回传，都走本地 ACP 行现有的那一条路径。

本期用三个环境验收：ravenx、ravenx2（同一台物理机上的两个容器）和本机。

## 非目标（第一期不做）

- 在网页上添加或展示远端 agent。`ui-web/` 不动；第一期从配置文件添加，第 3 个 PR 再给 `subagents.add` 加 `machine` 参数。
- 把 MCP 服务器借给远端 agent。第二期考虑用 `ssh -R` 转发 socket。
- 把远端产出回传到本机，包括文件清单和 desk diff。
- 把附件、DAG 上游文件路径送给远端 agent。这部分沿用 `reads_local_files=False` 的现有降级。
- 把 Raven 持有的 API key 借给远端 agent（`lendKeys`）。
- 远端 agent 是 Raven 自己（`raven acp`）时的特殊支持。它可以作为普通 ACP 命令来用，但父模型绑定不会带过去。
- Raven 之间互联：那是 TODO 第 4 条，走 A2A。
- 网络中断的快速发现。本期依赖 ssh 心跳，约 45 秒。

## 约束

编号一经使用就不再改。

### 架构

| # | 约束 | 怎么检查 |
|---|---|---|
| C1 | 远端只改变传输的另一端，`acp_agent.run`、`pool`、`AcpClient` 走同一条路径。会话、取消、权限、流式输出、回复格式不分叉。 | 远端行没有自己的 `run()`；分支只出现在"启动行、会话目录、MCP、文件账、说明文字"五处。 |
| C2 | ssh 的地址、端口、用户、密钥只从登记表来。拼好的启动行不写盘、不进日志正文，也不进回复、run record、roster 或 RPC 结果。 | 单测：在这些出口里查登记行的 host、port、key，结果为空。 |
| C3 | 远端命令有两层 shell：sshd 起的那一层，和 agent 所在的登录 shell。每一层的引号只加一次，也只被对应的那一层解析一次。agent 命令本身按 shell 命令行解释，和本地行一致。 | 单测：环境变量值带空格、引号、`$` 时，远端进程看到的值不变。实机跑 `qwen --acp`，确认 `--acp` 没有丢。 |
| C4 | ssh 选项与 `raven/ops/transport.py` 是同一份。从 `make_ssh_runner` 里抽出 `ssh_argv`（选项）和 `ssh_target`（从登记行读出地址、端口、密钥、用户），runner 和 ACP 共用。ACP 另加 `-T`、`ServerAliveInterval=15`、`ServerAliveCountMax=3`。 | `make_ssh_runner` 的现有测试不变且通过；新测试钉住它发出的 argv。ACP 启动行的单测断言这几个选项都在。 |
| C5 | 远端 agent 行在本机的启动目录固定（Raven 的 home），不随调用方的工作区变化。 | 单测：两个不同工作区派给同一远端行，池里的 launch key 相同，只起一个 ssh。 |

### 设计

| # | 约束 | 怎么检查 |
|---|---|---|
| C6 | 发给 `session/new` 和 `session/load` 的 cwd 一律是远端会话目录的绝对路径。它在打开会话前，由一次 ssh 调用 `mkdir -p` 建好，并用 `pwd -P` 解析出来。本机工作区路径永远不发给远端。 | 单测：截下发出的帧，cwd 等于解析出的远端路径。实机：claude-agent-acp 接受这个路径。 |
| C7 | 远端 agent 行一律按 `reads_local_files=False` 处理。附件和上游文件路径走现有的"无法送达"说明，roster 标 `no-local-files`。 | 单测：`agent_meta` 对远端行返回 False。 |
| C8 | 远端 agent 行不借 MCP：不发 bridge stanza，回复末尾加一条 `[raven]` 说明，roster 的可注入 MCP 为空。 | 单测：`session/new` 帧里的 `mcpServers` 是 `[]`，回复里有这条说明。 |
| C9 | 带到远端的环境变量只有该行的 `env` 和 `RAVEN_SUBAGENT=1`，拼成 `env K=V ...` 放进远端命令。`RAVEN_HOME`、本机登录 shell 环境、父模型绑定、借出的 key 一律不带。写入配置时，带 `lendKeys` 的远端行被拒绝。 | 单测：断言远端命令里只有这几项。写入校验的单测。 |
| C10 | 远端 agent 在那台机器用户自己的登录 shell 里启动（`exec "$SHELL" -lic ...`），PATH 和登录状态都用那台机器自己的。 | 实机：nvm 装的 qwen、`~/.local/bin` 下的 claude 都能找到。 |
| C11 | 文件账：远端行不做本地快照，不做删除推断，`files` 为空。回复和派发通告里写明"在 <机器显示名> 的 <会话目录>"。 | 单测：远端行一轮写文件之后，`RunActivity.files` 为空，通告里有机器名和目录。 |
| C12 | ssh 本身失败（退出码 255，或握手前退出）时，按类别给一句话：机器未登记、连不上、超时、认证被拒、主机密钥变了、对方没有这个命令。这句话不带 ssh 原文，也不带地址。ssh 自己的输出用 `-E` 写进仅属主可读的日志文件（每条 agent 行一个），不进 stderr，因此也不进连接日志和报错里引用的 stderr 末尾；只在归类失败时读回。 | 单测：对每类 stderr 样本断言输出的句子，并断言句子里没有 host 和 port。 |
| C13 | `subagents.list` 的免费探测对远端行不发 ssh，只看两件事：机器在不在登记表里，以及已记录的能力快照。Test 和 Connect 走真实 ssh 握手，`verify_agent` 和 `ping_agent` 用远端会话目录。 | 单测：免费探测不调用 ssh。实机：Test 能分出"连不上 / 没装 / 没登录 / 正常"。 |
| C14 | 取消沿用 `session/cancel`。关闭连接时杀本机 ssh，远端进程靠 stdin 收到 EOF 自行退出。不用 `-tt`。 | 实机：见 A3、A4。 |

### 假设

| # | 假设 | 状态 |
|---|---|---|
| C15 | 本机 ssh 被 SIGKILL 后，远端 agent 会自行退出，执行工具中途也一样。 | 2026-10-10 已实测：测试桩立即退出；claude-agent-acp 空闲时 15 秒内退出，执行 `sleep` 中途 2 秒内连同子进程一起退出。qwen 因未登录没有测到，验收时补。 |
| C16 | `bash -lic` 不往 stdout 写东西。 | 2026-10-10 在两台机器上实测为 0 字节。即使用户的 rc 文件有输出，Raven 的读循环也会跳过非 JSON 行（测试桩的 `noisy` 模式覆盖了这一点）。 |
| C17 | agent 会拒绝不存在的 cwd。 | 2026-10-10 已实测：claude-agent-acp 返回 `-32602`，说明"cwd does not exist on the machine running the agent"。 |
| C18 | ssh 不会转发环境变量。 | 2026-10-10 已实测：传给本机 ssh 的变量，远端没有收到。 |
| C19 | 同一台物理机同时进 10 路 ssh 没有问题。 | 2026-10-10 已实测：两台机器 10 路，0.29 秒全部成功。sshd 默认 `MaxStartups 10:30:100`，超过 10 路同时握手可能被随机拒绝，本期按 10 路以内设计。 |

## 验收清单

★ 标的是要由人来跑的项目。

| # | 结果 | 证据 |
|---|---|---|
| A1 | 给一条 Claude Code 行加上 `machine: ravenx2` 后，主 agent 能把任务派给它，它在 ravenx2 上干活。 | 会话目录里有它写的文件；回复里写明机器名和目录。 |
| A2 ★ | 同一轮里把 5 个任务派到三个环境（ravenx、ravenx2、本机），5 个都完成，结果都回到主 agent。 | 5 条结果通告；墙钟时间接近最慢的那一个，而不是五个相加。 |
| A3 ★ | 中途取消，5 个都停下；60 秒后远端 `ps` 里没有这次起的进程。 | 取消前后的远端 `ps` 对比。 |
| A4 | 停掉 Raven（`raven web --stop`）后，远端不留进程。 | 远端 `ps`。 |
| A5 ★ | 有一台连不上（用 work2）：那一个任务在连接超时内失败，并说明是哪台机器、哪类原因，其余任务照常完成。 | 失败通告的原文里没有地址，也没有 ssh 原始输出。 |
| A6 | 远端 agent 没登录（ravenx2 上的 qwen）：失败说明是"要在那台机器上登录"，不是超时；其余照常。 | 失败通告。 |
| A7 | 远端行的 MCP 不借出，回复里说明一次。 | 回复末尾的 `[raven]` 行。 |
| A8 | 附件不送到远端，按现有文案说明一次。 | 派发回执。 |
| A9 | 两个并行任务落到同一台机器时，各用各的会话目录，不互相覆盖。 | 远端两个目录各有自己的文件。 |
| A10 | 同一个 instance 第二次派发，回到同一个会话目录、同一个会话。 | 第二次回复引用了第一次的内容；目录相同。 |
| A11 | 主 agent 看到的 agent 列表能分辨机器，但看不到地址。 | roster 文本。 |
| A12 | 对远端行按 Test 是真实握手，能分出连不上、没装、没登录、正常四种结果。 | 四种情况的 Test 结果。 |
| A13 | 本地行的行为不变。 | 全量测试通过。 |
| A14 | 启动行、主机地址、端口、密钥路径不出现在日志正文、回复、run record 和 RPC 结果里。 | grep 这几处。 |

## 设计

### 改动地图

| 层 | 现在 | 之后 |
|---|---|---|
| 配置 `ThirdPartyAcpSubagentConfig` | `command`、`cwd`、`env` | 加 `machine`（机器 id）和 `remoteCwd`（远端根目录）。指纹 `_LAUNCH_FIELDS` 和 `_ACP_FIELDS` 把这两项算进去。 |
| `raven/ops/transport.py` | ssh argv 写在闭包里 | 抽出 `ssh_argv(host, port, key, *, ..., extra=())` 和 `ssh_target(row)`，runner 改为调用它们，发出的命令行不变。 |
| `raven/ops/connections.py` | 没有按 id 查一行的函数 | 不改。按 id 查机器放在调用方 `remote.machine()` 里，与 `machine_exec._runner_for_connection` 的做法一致（主干只保留调用方需要的读取函数，见 0fd38ef85）。 |
| 新文件 `raven/acp_client/remote.py` | 无 | 按 id 取机器（拒绝不存在、不可用和本机的行）；拼启动行；用一次 ssh 建会话目录并解析绝对路径；把 ssh 失败归类成一句话。 |
| `acp_agent.run` / `_open_session` | `launch_cwd`、`session_cwd` 是本机路径；带 MCP bridge；做本地快照 | 远端行：启动目录固定，用远端会话目录，不借 MCP（加说明），不做文件账，不带父模型绑定。 |
| `capabilities.verify_agent`、`probe._probe_acp`、`ping_agent` | 在本机 `which`；会话 cwd 是本机临时目录 | 远端行：免费探测只看登记表；握手走启动行；会话 cwd 用远端目录。 |
| `backends/__init__.py`（`agent_meta`、roster） | acp 行一律 `local-files` | 远端行改为 `no-local-files`，并加 `on <机器 id>` 标签。 |
| `manager._announce_result` | `Working directory:` 是本机路径 | 远端行改为 `<机器 id>:<会话目录>`。 |
| `update_subagents` 校验 | 无 | 远端行带 `lendKeys` 时拒绝；`machine` 不在登记表时只告警，不拒绝（登记表可以晚一步写）。 |

### 启动行怎么拼

以 ravenx2 上的 Claude Code 为例。配置行：

```json
{"kind": "acp", "name": "claude_code@ravenx2", "preset": "claude_code",
 "command": "npx -y @agentclientprotocol/claude-agent-acp@0.81.1",
 "machine": "ravenx2", "remoteCwd": "~/raven-work"}
```

启动时，从登记表取出 `ravenx2` 这一行，拼成：

```
ssh -i <key> -p <port> -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new
    -T -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -E <ssh 日志> <user>@<host> -- <远端命令>
```

远端命令是一个 argv 元素，内容为：

```
mkdir -p <根> && cd <根> && exec "${SHELL:-/bin/sh}" -lic 'exec env RAVEN_SUBAGENT=1 K=V ... <agent 命令>'
```

几点说明：

- 根目录里的 `~` 由远端 shell 展开，所以根目录这一处不加引号保护 `~`，其余部分都经 `shlex.quote`。
- `ssh` 进程本身仍由 `AcpClient.launch` 启动。它对 `command` 做 `shlex.split`，所以这里传入的是 `shlex.join(argv)`，`AcpClient` 和 `pool` 都不用改。
- 启动日志只打印 `argv[:1]`，也就是 `ssh`，现状就是这样。
- `-E` 把 ssh 自己的话（含地址，例如 "connect to host <ip> port <n>"、首次连接时的 "Permanently added '[<ip>]:<n>'"）写进 `<日志目录>/ssh/<agent 名>.log`，目录权限 0700。2026-10-10 实测：加了 `-E` 之后 stderr 只剩远端自己的输出。

### 会话目录

- `run()` 已经算出 `handle = instance or task_id`，会话目录就是 `<远端根目录>/<handle>`。
- 打开会话前，先用 `make_ssh_runner` 跑一次 `mkdir -p <目录> && cd <目录> && pwd -P`，拿到绝对路径，这一步约 0.3 秒。
- 按"机器 + handle"缓存这个路径，所以同一 instance 第二次派发不再多跑这一趟。
- `session/load` 也用这个路径，满足 A10。

### 失败归类

ssh 失败时，退出码是 255，stderr 是 ssh 自己的话。归类方法：

| stderr 里出现 | 说明 |
|---|---|
| `timed out` | 连接超时 |
| `Connection refused`、`No route to host`、`Could not resolve` | 连不上 |
| `Permission denied` | 认证被拒 |
| `REMOTE HOST IDENTIFICATION HAS CHANGED`、`Host key verification failed` | 主机密钥变了 |
| 远端 shell 报 `command not found`（退出码 127） | 那台机器上没装这个 agent |

其他情况统一为"连接在握手前断了"。所有说明都带机器 id 和显示名，不带地址。

登录问题不在这一层：agent 的 `session/new` 返回认证错误，走现有的 `sign_in_hint_for` 路径，只是说明里加上"在 <机器> 上"。

### 并发

连接池按 agent 名字分连接，所以一条远端行对应一个 ssh 进程，这一行的多个会话复用它。claude-agent-acp 支持一个连接上开多个会话。

5 个任务分到三个环境，靠的是三条行：`claude_code@ravenx`、`claude_code@ravenx2` 和本机的 `claude_code`。并发上限仍是 `max_concurrent_subagents`，默认 8。

## 代价

- **谁承担：** `acp_agent.run` 多一处"远端行"分支，涉及启动行、会话目录、MCP、文件账、说明文字五处。另有一个新文件 `remote.py`，约两三百行。
- **别人要付的：** `transport.py` 抽出 `ssh_argv` 和 `ssh_target` 两个函数，行为不变。这个文件主要由 silverLXT 维护。
- **能不能撤回：** 能。不带 `machine` 的行走原路径，撤回就是 revert。
- **单独上线没价值的一半：** 只有启动行、没有会话目录时，claude-agent-acp 会直接拒绝建会话（C17），所以两者必须同一个 PR 上。

## 决定

**D1. 直接 ssh 起第三方 agent，不在每台机器上装 Raven。**
摸底时直连已经跑通。ravenx 上是 Python 3.9，Raven 要求 3.12 以上，每台都装 Raven 本身就是一项工作。
否决的方案：每台跑 `raven acp`。它能顺带解决 MCP 和环境变量的问题，作为以后的选项保留。

**D2. 机器信息只从登记表来，配置里不写 ssh。**
否决的方案：让用户在 `command` 里手写 ssh。摸底实测，手写时 `--acp` 被远端 shell 吞掉，结果是 60 秒超时、没有任何提示。而且地址会进配置文件和页面。

**D3. 每个 handle 一个会话目录。**
否决的方案：
- 一条行只用一个目录：并发任务会互相覆盖。
- 用和本机工作区同名的路径：远端不存在，还会暴露本机的目录结构。

**D4. 环境变量拼进远端命令。值会出现在两端的进程列表里，所以只放非机密的值，并拒绝 `lendKeys`。**
否决的方案：
- `SendEnv`：要改每台机器 sshd 的 `AcceptEnv` 配置。
- 在 stdin 上先发一段变量再交给 agent：依赖具体 shell，留到以后。

**D5. 第一期不借 MCP。**
否决的方案：
- 照发本机的 stanza：实测 qwen 只在 stderr 打一行警告，用户看不到工具少了。
- `ssh -R` 转发 socket：远端需要有能执行 `raven mcp bridge` 的东西，放第二期。

**D6. 不用 `-tt`。**
伪终端会改写 JSON 流（换行转成 CRLF、回显输入）。断线清理已经靠 EOF 实测通过（C15）。

**D7. 本机走现有的本地路径。**
通过 ssh 连本机是可选项，需要在系统设置里打开"远程登录"。打开后，可以用它来测 zsh 和 macOS 的差异。

## 交付顺序

| PR | 内容 | 之后能做什么 |
|---|---|---|
| 1 | 本稿；`transport.ssh_argv` / `ssh_target`；`remote.py`（取机器、启动行、会话目录、失败归类）及其测试。 | 还没有调用方。启动行和会话目录在本机 shell 和 ravenx2 的 dash 上实跑过。 |
| 2 | 配置字段 `machine`、`remoteCwd`；`acp_agent.run`、探测、roster、派发通告接上 `remote.py`。 | 改配置文件就能把任务派到已登记机器上（A1-A14）。 |
| 3 | `subagents.add` 和 `raven_config` 的 `machine` 参数。 | 在对话里说"把某台机器上的某个 agent 接进来"。 |

## 已定问题

- **验收口径（2026-10-10，owner）：** 同一台物理机上的容器不算"设备"。本期先用三个环境（ravenx、ravenx2、本机）做通；按指标验收"5 台设备"时，A2、A3 要在 5 台独立设备上补跑。
