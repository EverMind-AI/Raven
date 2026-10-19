# 在登记机器上工作

把子 Agent 放到另一台电脑上跑：GPU 机器、实验室工作站、办公室的服务器，
而对话仍在你自己的 Raven 里。登记一次机器，把 Raven 的节点装上去，再在 Agent
条目里写上这台机器，之后 Agent 就派到那里执行，在那里的目录里工作，文件也留在那里；
对话按地址读取，你要的时候再取回一个。连接断了，或者你把 Raven 停了，Agent 照样工作，
回来时接回结果。

本页是完整的操作指南。委派本身见[与子 Agent 协作](agent-collaboration.md)，
Agent 的安装见[Agent 集成](agent-integrations.md)。

## 准备条件 { #what-you-need }

- 一台能用密钥 ssh 登录的机器，登录用户就是 Agent 将要运行的用户。Raven 不会输入密码，
  登录必须只凭密钥就能完成。
- 机器上是 Linux 或 macOS，该用户有 POSIX shell。节点依赖 unix 套接字和进程组，
  所以 Windows 机器不能作为目标。（Raven 本机可以是 Windows，见[限制与默认值](#limits-and-defaults)。）
- 机器上有 `uv`（在 PATH 上，或在它的安装程序放置的位置），**或者**有 `curl` 或 `wget`
  让安装程序去取一个。安装程序用 `uv` 构建节点，没有 `uv` 就停下，不管机器上有什么
  Python。机器上有 Python 3.12 或更新版本更好，但不是必需：有的话，节点的虚拟环境从它
  建出来；没有的话，`uv` 自带一个 Python 3.12，占用空间更多。
- 要运行的 Agent 已经装在那台机器上，并且以那个用户登录过：Claude Code、Codex、
  Qwen Code 这类 ACP Agent，命令行 Agent，或者一个只在那台机器网络里能访问的
  OpenAI 兼容端点。

## 1. 登记机器 { #1-register-the-machine }

```bash
raven ops connection add
```

命令一次问一件事：你怎么称呼这台机器、它是 ssh 连的还是这台电脑本身、给它一个短 id，
然后是地址、端口、用户名和私钥路径。写入之前会先连一次；连不上的机器不会被记录。
机器能自己说清楚的信息（核数、内存、GPU）从机器上读，不用手敲。最后几个问题是给
on-call 任务用的：装了什么软件（随便写一句即可）、预算单位和同时跑几个任务（回车取默认值）、
存放你工作的目录（可留空）。你列出的目录以后读文件也不用再问（见[第 6 节](#6-read-edit-and-fetch-by-address)）。

写脚本时，把每个回答都用参数给出：

```bash
raven ops connection add --id gpu1 --name "GPU box 1" \
  --host <地址> --port 22 --user alice --key ~/.ssh/id_ed25519 \
  --non-interactive
```

记录了什么、记在哪里：

- 这一行写进 `connections.json`：`RAVEN_CONNECTIONS` 指向的文件；没有就是 config
  旁边那份；再没有就是 Raven 主目录（`~/.raven`）里那份。注册表的细节见
  [Oncall](oncall.md#prepare-the-agent-and-machine)。
- 记录的是私钥的**路径**，从不记录私钥本身。
- 只能用密码登录的机器，在 Agent Hub 里把**登录方式**选成**密码**来登记。密码只用于这一次
  ssh 登录：Raven 在这次登录里把自己生成的一把密钥（`ssh-keygen` 生成，存在
  `~/.raven/keys/<id>_ed25519`）写进该账号的 `authorized_keys`，之后每次连接都用密钥，
  登记表里记的也是这把密钥的路径。密码不保存、不进日志、不回显。机器的 sshd 必须允许
  密码登录（`PasswordAuthentication yes`）这一步才能过，不允许的机器会拒绝，拒绝语里会说明。
  本机是 Windows 时不提供。公钥单独占一行，接在 `authorized_keys` 原有内容之后；随后的
  检查和这次登录一样读你的 ssh 配置，所以经跳板或别名才能到达的机器也照样登记。移除机器时
  会把这把密钥收回（[第 8 节](#8-remove-a-machine)）；登录之后才被拒绝的登记会把它留在
  机器上（以 `raven@<id>` 结尾的那一行），下次登记这台机器时继续用。
- 机器的主机密钥在第一次连接时接受并写进 `~/.ssh/known_hosts`。之后机器换了密钥，
  会被拒绝，直到你自己核实过。用密码登记时，这第一次连接带着密码：在不信任的网络上，
  先在终端里连一次（`ssh user@host` 会显示指纹让你确认）再登记，或者改用密钥登记。
- `~/.ssh/config` 里会写入一段受管理的 `Host <id>`，这样本机的 `scp` 和 `rsync`
  可以按 id 找到这台机器。

`raven ops connection list` 列出机器。`raven ops connection doctor` 逐条列出条目的问题，
阻塞的和提示性的都列，但只有一台可用的机器都没有时退出码才非零：一条坏的条目旁边有一条
可用的，退出码仍是零，所以要看它的输出，不要只看退出码。

### 在 Agent Hub 里 { #from-the-agent-hub }

不开终端也能登记：Agent Hub 顶部与**智能体**、**渠道**并列的**机器**标签列出已登记的机器，
**登记机器**打开一个表单，
填名称、id、地址、端口、用户名和私钥路径，以及工作目录。页面和命令一样，写入之前先连一次，
连不上的机器不会被记录；拒绝时会说明是哪一类（连不上、拒绝登录、主机密钥变了、没有按时应答）
以及该怎么办。之后页面上显示的只是这台机器是什么——名称、id、链路、节点——从不显示地址、
用户名或私钥路径：这些只在表单里传一次，不会再读回来。

## 2. 安装节点 { #2-install-the-node }

```bash
raven ops connection install-node gpu1
```

节点是 Raven 自己的代码，装在机器上的一个小虚拟环境里，负责在那里启动 Agent、回答文件调用。
命令先看一眼机器，打印它准备做什么，确认后才动手（`--yes` 跳过确认）。成功时最后两行是：

```text
Installed the Raven node <版本>-<摘要> in /home/alice/.raven-node (62 MB).
The node answers: Python 3.12.3, code <摘要>.
```

机器上会多出什么：

| 内容 | 位置 |
| --- | --- |
| 节点 | `<node_dir>/<版本>-<摘要>/`，`node_dir` 取注册表那一行的值，默认 `~/.raven-node` |
| 运行状态 | `node_dir` 下的 `run/`（套接字）、`tasks/<任务>/`（每个 Agent 的记录，仅属主可读）和 `log/` |
| `uv`（机器上原本没有时，或者机器自己的 `uv` 早于 0.4.23 而 pip 又配了索引时——那样的 `uv` 会无视索引） | `<node_dir>/.uv`，版本 0.13.0，用机器自己的 `curl` 或 `wget` 取回；之后的安装直接用它 |
| Python 3.12（机器上原本没有时） | 节点旁边，由 `uv` 管理 |

别的都不变：不装系统包，不改 shell 的 rc 文件，不改环境变量，不注册服务，不开端口。
节点的依赖包从机器上 pip 配置的索引安装，没有配置就从 PyPI 安装。安装程序按下面的
数字检查空间：

| 部分 | 大约 |
| --- | --- |
| 节点 | 60 MB |
| `uv` 带来的 Python 3.12 | 再加 80 MB |
| `uv` 本身（需要取回时） | 再加 25 MB |

`--dir <路径>` 把节点放在 `~/.raven-node` 以外的地方；路径会记进注册表那一行，
以后的命令都能找到。机器上已经有这个 Raven 的节点时，什么都不动。节点的名字也会记在
那一行（`node_install`），Agent Hub 就是靠它在链路还没建立时说明机器上有节点。

### 在 Agent Hub 里安装 { #installing-from-the-agent-hub }

机器卡片或抽屉里的**安装节点**在后台做同样的安装：卡片上显示*正在安装节点*直到结束，
抽屉里保留结束时的结果。抽屉里的*安装到*就是命令的 `--dir`。节点是另一个版本的 Raven 装的，
会显示*需要更新*并提供**更新节点**。把 Agent 接到某台机器上，在它预设的抽屉里选*运行在*：
列出的是全部已登记机器，只有装了本 Raven 节点的能选（其余会说明原因），acp 和命令行
Agent 还要填机器上的工作目录。

**升级。** 节点跑的正是装它的那个 Raven 的代码，每次连接都按摘要核对。在本机更新
Raven 之后，再跑一次 `install-node`：新节点装在旧节点旁边；`--prune` 删掉目录里其他
版本的节点（缓存的 `uv` 和 Python 保留）。没升级之前，派到那台机器会失败，提示
*runs different code from this Raven; update it with `raven ops connection install-node gpu1`*。

**守护进程。** 安装不会启动任何东西。Raven 第一次使用这台机器时，它的 ssh 会话会把
节点的守护进程拉起来，守护进程不随会话结束；没有连接也没有 Agent 在跑满 10 分钟后，
它自己退出。机器重启后，下一次使用会再把它拉起来。

## 3. 在 Agent 条目里写上机器 { #3-name-the-machine-in-an-agent-entry }

拿配置里 `subagents.agents` 下已有的那条 Agent 条目（子 Agent 页面或 `raven_config`
添加的那条），加上 `machine`，值是注册表里的 id。`remoteCwd` 是 Agent 在那台机器上的
工作目录，默认 `~/raven-work`，`~` 是登记用户在那台机器上的家目录。每个实例或任务在
它下面各有自己的目录：`<remoteCwd>/<实例或任务 id>`。

### ACP Agent { #acp-agents }

```json
{
  "name": "Claude Code on gpu1",
  "preset": "claude_code",
  "kind": "acp",
  "enabled": true,
  "command": "<该 Agent 的 ACP 命令，同本机条目>",
  "machine": "gpu1",
  "remoteCwd": "/data/alice/projects"
}
```

节点在机器上启动条目的 `command`，用的是登记用户的登录环境（`bash` 或 `zsh` 登录时
读的 profile 和 rc 那一串；其他 shell 的用户得到的是 ssh 启动节点时的环境）。条目的
`env` 叠在上面，经链路送到 Agent，不走命令行。`cwd` 指的是本机目录，机器上的条目不读它。

### 命令行 Agent { #cli-agents }

```json
{
  "name": "my-cli on gpu1",
  "kind": "cli",
  "enabled": true,
  "command": "my-agent --prompt-file {prompt_file}",
  "machine": "gpu1"
}
```

同一条命令在机器上执行，按那台机器 POSIX shell 的规则切分。提示词送达的方式和本机一样有
三种：`{prompt}` 作为一个参数、`{prompt_file}` 写成那次运行目录下的文件、或者走标准输入
（节点送完提示词就关闭它）。输出按行原样回传，空行也在内。取消或超时会结束机器上的整棵
进程树，放到后台的命令也一起结束。

### OpenAI 兼容端点 { #openai-compatible-endpoints }

```json
{
  "name": "vLLM on gpu1",
  "kind": "openai",
  "enabled": true,
  "baseUrl": "http://127.0.0.1:8000/v1",
  "model": "my-model",
  "machine": "gpu1"
}
```

用于只在那台机器上、或只在它的网络里能访问的端点。每一轮都开一条到机器的 ssh 隧道，
请求经隧道送到机器所理解的 `baseUrl`，这一轮结束隧道就关。`baseUrl` 必须是 `http`：
隧道的终点是本机回环地址，`https` 证书对不上它。这种类型没有 `remoteCwd`（端点没有
工作目录），写了会在加载时丢弃并给出警告。

### 内置 Agent { #built-in-agents }

内置 Agent 是 Raven 自己的循环，跑在这个进程里，模型密钥和工具都在这里，没有可以放到
别处启动的进程。内置条目上的 `machine` 在加载时丢弃并给出警告，条目仍在本机运行。

## 4. 或者在对话里添加 { #4-or-add-one-from-a-conversation }

在对话里直接说，比如“把 Codex 预设添加到机器 gpu1 上”。主 Agent 会列出可用的登记机器，
然后带着预设、机器，以及（ACP 或命令行预设可选的）`remote_cwd` 调用 `subagents.add`：

```json
{"preset": "codex", "machine": "gpu1", "remote_cwd": "/data/alice/projects"}
```

不指定名字时条目叫 `<预设>@<机器>`，所以一个预设可以在每台机器上各添加一次。存入之前，
添加会像本机添加一样先验证：用那个 Agent 在那台机器上真的跑一条测试提示，所以 Agent
必须已经在那里装好并登录。预设是内置 Agent、OpenAI 预设带了 `remote_cwd`、或者机器的
条目不可用时，添加会在花费任何东西之前被拒绝。

子 Agent 页面上的**添加**暂时不能指定机器；要指定机器，用对话或者改配置文件。

## 5. 派发时会发生什么 { #5-what-happens-when-you-dispatch }

照常派发：让宿主委派，或者和这个 Agent 的实例直聊。和本机 Agent 相比有三处不同。

**附件只给地址，不复制。** 随任务来的文件按它在本机上的地址告诉 Agent，并注明没有复制。
Agent 在机器能读到的地方使用它（两边都挂载的盘上的同一路径、已经在那里的副本），
读不到就在回复里说明找不到，然后继续。

**MCP 服务器不出借。** 宿主的 MCP 服务器通过本机上的一个套接字访问，别的机器上的
Agent 打不开它。授权了的服务器会在回复里注明没有出借。跨链路出借即将提供（拉取请求 #947）。

**它写的文件留在机器上。** 这一轮结束时什么都不会自动拿回来。回复末尾有一行说明它在哪里工作：

```text
[raven] Ran on machine 'GPU box 1' (gpu1), in alice@203.0.113.7:/data/alice/projects/task-3f9a;
the files it wrote are there, not on this computer -- read_file, list_dir, find,
grep, write_file and edit_file take that path as it is, and deliver_files fetches one by it.
```

派发记录里也用同样的方式写目录：*Working directory: on machine 'gpu1',
/data/alice/projects/task-3f9a (made there when the run started)*。

### 机器上的路径怎么写 { #how-a-path-on-a-machine-is-written }

机器上的路径按 `scp` 的写法写，带注册表里的用户、地址和端口。回复、记录、工具结果里都
只用这一种写法，工具也按这种写法读回去：

| 形式 | 例子 | 何时 |
| --- | --- | --- |
| `user@host:/abs/path` | `alice@203.0.113.7:/data/alice/projects/task-3f9a/report.md` | 端口是 22 |
| `scp://user@host:port//abs/path` | `scp://alice@203.0.113.7:2222//data/alice/projects/task-3f9a/report.md` | 其他端口；注意绝对路径前是两个斜杠 |
| `<id>:/abs/path` | `gpu1:/data/alice/projects/task-3f9a/report.md` | 你自己输入时可以这样写：注册表 id 可以代替地址 |

冒号后面不以 `/` 开头的路径，相对于那台机器上用户的家目录。本机上名字里带冒号的文件，
写成 `./a:b` 或绝对路径。对不上任何登记机器的写法会在任何 ssh 之前被拒绝，并说明原因。

## 6. 按地址读、改、取 { #6-read-edit-and-fetch-by-address }

`read_file`、`list_dir`、`find`、`grep`、`write_file` 和 `edit_file` 接受机器上的路径，
和本机路径一样用。调用通过节点在机器上执行，只有结果回传：读 50 行就只传这 50 行。
行号、上限和页脚和读本机文件完全一样。

- **读不用问**的范围是 Raven 的 Agent 在那台机器上的工作目录：`~/raven-work`、那台机器上
  每个条目的 `remoteCwd`，以及注册表那一行里列出的绝对路径 `paths`。读别处会询问，
  就像在本机读工作区以外的文件一样。
- **写一律询问**，和本机一样，授权只对那一个文件有效。整文件写入会核对文件是否还是这个
  对话上次读到的样子；中途被改过的文件不动，并提示 *changed after it was read;
  nothing was written. Read it again before writing.*
- 节点把每次调用限制在审批允许的目录内，并按机器自己的符号链接解析。

`deliver_files` 按地址取回一个文件，这样你就能在页面上下载它。文件落在对话工作区下的
`pulled/<机器>/<键>/<文件名>`，键是它在机器上路径的摘要，所以两个路径永远不会互相覆盖。
只取 Agent 工作目录之内的文件，而且不超过 20 MiB；更大的文件或别处的文件，在机器上读。
`pulled/` 不会自动清理，不需要时自己清掉。

在 `exec` 里直接输入指向登记机器地址的 `ssh` 会被拒绝，并提示应走的登记路线；
`scp` 和 `rsync` 到它没有限制，而且因为登记时写了别名，可以按 id 找到它。

## 7. 断线与重启 { #7-disconnects-and-restarts }

Raven 对每台机器保持一条 ssh 链路，用心跳看着它：每 5 秒 ping 一次，15 秒没有回应就算
链路断了。Agent 没有断：它们在机器的守护进程下继续运行，写出的每一帧和 stderr 的每一行都被守护进程保存。
只要还有 Agent 在等，Raven 就按 2、4、8、16 秒的退避不断重试；链路回来后按任务 id
把每个 Agent 接回，从 Raven 上次读到的那一帧和那一行 stderr 继续，这一轮像什么都没发生一样进行下去；
链路断开期间发出的取消，链路回来时送达。

停止 Raven 的效果是一样的，而且是有意为之。机器上的 Agent 留给守护进程，Raven 这边
留下的是 `<Raven 主目录>/machines/runs/` 下的一条记录。下次启动时读这些记录，把每次
运行从原来的位置接回，连同这一轮写在 stderr 里的内容一起读到（命令的整个 stderr：失败的原因，或只在 stderr
里报会话 id 的 CLI 给出的 id），再把结果通告到发起它的那个对话，就像一轮正常结束一样。为此要保留
机器的登记和 Agent 条目。`/restart` 和被强制结束的 Raven 也按同样方式处理。

接不回来的情况：机器重启过，或者守护进程被手动结束，任务就不再被机器认识。这一轮会以错误
通告（*no longer runs 'agent' (task …)*），机器上也不会留下任何在跑的东西。

一条 10 分钟内没有调用、也没有 Agent 的链路，会在 Raven 下一次使用别的机器时被关闭；
没有定时器，所以最后那条在用的链路会一直开着，直到那时。守护进程有定时器：没有连接也没有
Agent 满 10 分钟就退出。两者都不是错误，下次使用会重新建立。

## 8. 移除机器 { #8-remove-a-machine }

```bash
raven ops connection remove gpu1
```

确认后把机器从注册表里去掉（`--yes` 跳过确认），并删掉 `~/.ssh/config` 里受管理的
`Host gpu1` 段。那一段若少了两行标记中的一行（被手改过），就原样不动、在警告里点名，由你自己删；
文件里别的内容一律不碰。还有**已接入**的 Agent 条目写着这台机器时会拒绝：这样的条目在下一次派发时会失败，
而且是在 spawn 已经回答"已启动"之后，所以要先断开这些条目（Agent Hub，或配置里的
`subagents.agents` 段），或者加 `--force` 照样移除，命令会说出哪些条目还指着一台不存在的机器。
已断开的条目不拦着移除，只会被点名：再接入它会被拒，直到用同一个 id 重新登记机器。
`node_dir` 下的节点留在机器上，要删就到机器上手动删掉那个目录。Raven 为这台机器生成的密钥
（用密码登记的）会被收回：用这把密钥最后登录一次，把它那一行从机器的 `authorized_keys`
里删掉，再删掉本机 `~/.raven/keys/` 下的这对密钥；机器连不上时那一行留在那里，命令会说明，
以 `raven@<id>` 结尾的那一行由你到机器上删。

Agent Hub 机器抽屉里的**移除机器**做同样的事，先弹确认；本机链路上还有 Agent 在这台机器上
跑时也会拒绝，等它们跑完或先停掉。之前的 Raven 进程留在机器上的运行
（[第 7 节](#7-disconnects-and-restarts)）在机器移出注册表之后不会再被接回。

## 限制与默认值 { #limits-and-defaults }

| 项目 | 值 |
| --- | --- |
| 一台机器同时运行的 Agent 数 | 8 个；第 9 个启动会被拒绝，提示 *already runs 8 agents; wait for one to finish* |
| 已结束任务的记录在机器上保留 | 7 天 |
| 心跳间隔 / 判定断链 | 5 秒 / 15 秒 |
| 重连退避 | 2、4、8、16 秒 |
| 空闲链路关闭 | 10 分钟没有调用也没有 Agent 之后，在下一次使用别的机器时关闭；没有定时器 |
| 守护进程退出 | 没有连接也没有 Agent 满 10 分钟；有定时器 |
| ssh 连接超时 | 15 秒 |
| 一次文件调用的回应 | 120 秒内，否则调用失败 |
| `deliver_files` 取回的文件 | 最大 20 MiB |
| 机器上的默认工作目录 | `~/raven-work` |
| 默认节点目录 | `~/.raven-node` |
| 给命令行 Agent 送提示词的方式 | `{prompt}`、`{prompt_file}`、标准输入 |

注册表里的 `concurrency` 和 `budget_unit` 管的是 on-call 任务，不管这里的 Agent。

**Windows。** 机器必须是 Linux 或 macOS。Raven 本机可以是 Windows：在 Windows 上，
`C:file` 这种盘符相对路径按本机路径读，不会被当成叫 `C` 的机器；取回的文件名里如果有
Windows 保留的字符，落地时该字符写成百分号编码。

## 排错 { #troubleshooting }

每个失败都是一句话，用机器的 id 和显示名称指代它，从不出现地址。ssh 自己的信息会带地址，
写在 Raven 主目录下仅属主可读的日志里（`logs/ssh/`）。

| 提示里写着 | 意思 | 怎么办 |
| --- | --- | --- |
| *is not registered; add it with `raven ops connection add`* | 没有这个 id 的条目 | 对照 `raven ops connection list` 检查条目里的 id |
| *is registered, but its entry cannot be used; run `raven ops connection doctor`* | 条目不完整，或 id 重复 | 跑 `doctor`，按它指出的修 |
| *is this computer; remove `machine` from the agent's entry* | 这一行的 transport 是 `local` | 去掉 `machine`，Agent 在本机跑 |
| *refused the login; check the key it is registered with* | ssh 密钥登录失败 | 手动试 `ssh -i <密钥> user@host`；密钥换了就重新登记 |
| *presented a host key that differs from the one on record* | 机器的主机密钥变了 | 核实机器，再清掉 `known_hosts` 里它那一行 |
| *could not be reached* / *did not answer within the connection timeout* | 网络或地址问题 | 检查地址、端口，以及 sshd 是否在运行 |
| *is not installed on machine …; install it with `raven ops connection install-node …`* | 那里没有节点 | 安装节点 |
| *runs different code from this Raven; update it with …* | 节点来自另一个 Raven 版本 | 再跑一次 `install-node` |
| *has no uv, and no curl or wget to fetch one with* | 安装程序需要 `uv`，机器上既没有它也没有取它的办法；有 Python 也不改变这一点 | 在机器上装 `uv`（提示里给了它的安装程序），或者装 `curl` 或 `wget`，再试 |
| *has uv 0.4.22, and only uv 0.4.23 or later installs from the package index pip is configured with there; with no curl or wget to fetch uv 0.13.0, upgrade uv there* | 机器上的 pip 配了索引，它的 `uv` 太旧、读不了索引（或者说不出自己的版本），安装程序又没办法取一个新的 | 在机器上升级 `uv`，或者装 `curl` 或 `wget` 让安装程序把 `uv` 取到 `node_dir` 下，再试 |
| *the agent's command was not found on machine …* | 那个用户在机器上没装这个 Agent | 在机器上安装并登录 |
| *already runs 8 agents; wait for one to finish* | 机器已到上限 | 等一等，或换一台机器 |
| *did not answer a heartbeat within 15s* | 链路断了 | 不用做什么，Raven 会重连；Agent 还在跑 |
| *no longer runs 'agent' (task …)* | 守护进程丢了这个任务（重启、被结束） | 重新派发 |
| *changed after it was read; nothing was written* | 这个对话读过之后文件被别人改了 | 重新读一遍再写 |
| *is over the 20.0 MB limit for a file brought back* | 超过 `deliver_files` 的上限 | 在机器上读，或用 `scp` 拷贝 |
| *an endpoint reached through the machine has to be http* | `baseUrl` 是 `https` | 用机器访问它的 `http` 地址 |
| *this command reaches … over raw ssh* | 在 `exec` 里直接 `ssh` 到登记的地址 | 改用 `exec(machine=…)` 或文件工具 |

## 安全 { #security }

- 私钥留在本机，注册表只记它的路径。由那一行拼出来的 ssh 命令行只在内存里，不写进任何
  配置、日志或模型可见的文字。
- 用户、地址和端口会出现在模型面前，因为机器上的每个文件都靠它们命名（上面的 `scp` 写法）。
  密钥和 ssh 命令行永远不会。
- 节点不开网络端口。它监听自己目录下的一个套接字，属主是登记用户，只有这个用户的 ssh
  会话能接到它。那里每个任务的记录也只有这个用户能读。
- 节点用 Raven 自己的文件工具执行文件调用，限制在审批允许的目录内。它不保存对话、
  不调用模型；你的模型密钥不会去到机器上。
- 机器上的 Agent 是那台机器自己装的、在那里登录过的。Raven 不借给它自己的密钥：
  写了 `machine` 的条目上，`lendKeys` 什么都不出借。它从你这里收到的只有条目的
  `env`，经链路送达。
