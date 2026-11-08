# 轨迹调试与回放

把真实失败转化为可检查的证据，必要时进一步形成确定性回归测试。
Tracing 记录执行过程；轨迹汇集相关 attempt、模型/工具工件、对话上下文和结果标注。
添加埋点时参考[追踪与埋点 API](tracing-api.md)，本页介绍如何保存证据、诊断和回放。

## 保存失败 Attempt { #capture-the-failing-attempt }

Tracing 默认启用，可由 `RAVEN_TRACING` 或 `tracing.enabled` 改变。
失败后再开启追踪，不能补回此前缺失的工件。使用与失败运行相同的安装/trace store，
包括可能存在的 `RAVEN_TRACING_DIR` 覆盖。

```bash
raven trajectory list
raven trajectory save ATTEMPT_ID
```

将 `ATTEMPT_ID` 替换为列表中的 ID。Trace ID 可解析为所属 attempt，因此保存它可能
收集多个轮次。对于另一 Agent 的 workspace，可使用
`save --workspace /path/to/agent/home` 定位 session 记录。

`save` 创建自包含 bundle，并 pin 该 attempt，防止普通清理删除。
它不会脱敏。Manifest 报告缺失工件/消息和是否包含 session 上下文；
缺少证据的 bundle 不一定能回放。

原始 bundle 保留在本地、不要进入 git。其中可能有源码、prompt、凭据、工具输出
和私有对话历史。

## 生成经审核的报告 { #produce-a-reviewed-report }

```bash
raven trajectory report ATTEMPT_ID --config /path/to/agent/config.json
```

使用被追踪 Agent 实际使用的 config，为已知 secret 脱敏提供依据。
命令重新打包当前证据，对副本脱敏，在审核/确认流程后生成本地可分享 tarball，
原始 bundle 保留。自动扫描通过不保证所有商业隐私都已删除。

结构化 bug package 示例：

```bash
raven trajectory report-bug ATTEMPT_ID \
  --description "The tool result was omitted from the next model request" \
  --expected "The next request includes the tool result" \
  --actual "The next request has no tool result"
```

它创建本地记录/包，不会创建 GitHub issue；只有当 `RAVEN_BUGREPORT_URL` 指向一个
Bug Report Receiver 时才上传（见[发送与接收问题报告](#send-and-receive-bug-reports)），
回执随后写入记录。分享前检查 findings 和包内容。
`--yes` 不代表敏感数据风险授权，不应习惯性使用 `--accept-risk` 绕过，
或宣称所有 token 都无害。

WebUI 中的**问题报告**对会话里选中的轮次运行同一条流程，只是决定在页面上做，而不是在终端提示里；
见[导出与问题报告](webui.md#export-and-report-a-problem)。

### 记住的脱敏裁决 { #remembered-redaction-decisions }

你在审核中给出的每个"保留"或"替换"回答都会记在本机 Raven home（`~/.raven`，
设置了 `RAVEN_HOME` 时为该目录）下的 `redaction_history.json`。下一次报告会回放
这些记录：已经裁决过的值会列为"由脱敏裁决历史决定"，不再重复询问；没有 TTY 的
运行也不再需要为这些值传 `--keep-findings` 或 `--redact-findings`（仍需
`--accept-risk`）。文件里只保存每个值的哈希和掩码样本。私钥条目每次都会询问，
通过 `--keep-findings` / `--redact-findings` 整批做出的决定不会被记住。

```bash
raven trajectory report-bug ATTEMPT_ID -d "..." --review-all   # decide every item again
raven trajectory redaction-history list                        # what is remembered
raven trajectory redaction-history forget 3f9a1c2b7d4e          # drop one decision by id
raven trajectory redaction-history clear --yes                  # drop all of them
```

WebUI 中的报告有意采用不同的规则：决策页上的每个决定都会被记住，包括默认的"全部保持"和批量按钮，
来源记为 `web`，因为页面把它询问的每个值都展示给了用户。

设置 `RAVEN_BUGREPORT_REDACTION_HISTORY=off` 可既不回放也不记录。读不了的历史
文件会在下一次保存前备份到同目录；在那之前每个条目都会询问。

## 发送与接收问题报告 { #send-and-receive-bug-reports }

### 接收器 { #the-receiver }

Bug Report Receiver 是一个小型 HTTP 服务：接收 Bug Report Package、校验、按日
落盘、返回回执。它只接收答对其挑战的上传（见[上传挑战](#the-upload-challenge)），
不提供列表或下载。谁能连上它，仍由它监听的网络决定。

```bash
raven trajectory receiver --save-dir /srv/raven-bugreports
```

| 选项 | 默认 | 含义 |
| --- | --- | --- |
| `--save-dir` | 必填 | 接收包的目录；以 0700 创建 |
| `--host` / `--port` | `127.0.0.1` / `18795` | 绑定地址；`--port 0` 自动选端口并打印 |
| `--trusted-proxy CIDR` | 无 | 可信反代网段，其 `X-Forwarded-For` 才被采信（可重复） |
| `--max-package-mib` / `--max-unpacked-mib` | 64 / 256 | 最大包体，以及校验时解压的最大体积 |
| `--max-daily-mib` / `--max-daily-count` | 2048 / 500 | 一个 UTC 日可接收的字节数与包数 |
| `--min-free-mib` | 1024 | 保留的磁盘剩余空间 |
| `--rate-burst` / `--rate-per-10min` | 10 / 30 | 一个地址可连续上传的次数，以及每十分钟恢复的次数；`--rate-burst 0` 关闭限速 |
| `--concurrency` | 4 | 同时处理的上传数；超出返回 503 |
| `--idle-timeout` / `--total-timeout` | 30 / 600 | 无 body 数据的秒数上限，以及单次上传的总秒数上限 |

包以原始字节落在 `<save-dir>/<YYYY-MM-DD>/<report-id>.tar.gz`，旁边的
`<report-id>.json` sidecar 保存回执、发送方地址和 `bugreport.json` 的分诊摘要
（reporter、描述、严重程度、完整度、脱敏分类），读者不解包即可分拣。摘要只保留
每个字段的前 1000 个字符和至多 16 条路径提示；sidecar 与它的包一起计入每日字节
预算。日期是上传
被准入的 UTC 日：跨午夜才完成的包留在开始的那一天，sidecar 的 `received_at` 记
录真实落地时刻。同一个包再发一次返回已存的回执；同一 report id 的不同内容会被
拒绝。保留与清理由运维负责——按日的目录让 `find -mtime` 式清理很简单。

### 上传挑战 { #the-upload-challenge }

接收器响应四种请求：

| 请求 | 应答 |
| --- | --- |
| `GET /v1/hello` | `{"service": "raven-bug-report-receiver", "protocol": 1}`；客户端在报告开始前用它探测 |
| `POST /v1/challenge` | `{"challenge": "...", "expires_in": 300}`：为一次上传签发的新挑战 |
| `POST /v1/bugreports` | 上传，在 report id 与 sha256 之外带上 `X-Raven-Challenge` 和 `X-Raven-Response` |
| `GET /healthz` | `{"status": "ok"}`，供运维探活 |

挑战由接收器只保存在内存里的密钥签名，所以接收器不需要记录发出过哪些挑战，重启后
旧挑战全部失效；挑战在 300 秒内有效，可以重复使用。Raven 客户端用内置在 Raven 中
的常量，对"挑战 + 包的 report id、sha256 与大小"计算 HMAC 作为应答。接收器在读取
body 之前就校验这两个头：缺失、伪造、过期或与包不符的应答一律返回 `401`，带
`WWW-Authenticate: Raven-Challenge`，错误码为 `unauthorized`（或
`challenge_expired`）；不写入任何文件，但这次尝试仍计入该地址的限速。

这是一道浅层门槛，不是身份鉴权：那个常量随源码公开、不可配置，也不标识任何人。

| 谁在尝试 | 结果 |
| --- | --- |
| 用 `curl`、脚本或扫描器直接发文件 | `401`，不落盘 |
| 抓到一次上传，换一个包重放 | `401`：应答绑定了 report id、摘要和大小 |
| 抓到一次上传，原样重放 | 至多拿到该包已存的回执 |
| 读过 Raven 源码的人（仓库是公开的） | 挡不住，这是预期的 |
| 用 Raven 本身发送垃圾 | 由包校验、限速和每日预算约束 |

接收器和客户端要一起升级。较旧的接收器没有 `/v1/hello` 与 `/v1/challenge`：新客户端
会在报告开始前提示对方不是 Raven 接收器，上传时报"the receiver does not issue
challenges"；两种情况下包都会保留。

### 部署 { #deployment }

接收器只说明文 HTTP，默认只绑 loopback。要让本机以外访问，必须放在终止 TLS 的
反代之后；绝不要把端口直接暴露到公网。可用的 nginx location——它覆盖整个
`/v1/`，因为客户端除了上传，还会请求 `/v1/hello` 和 `/v1/challenge`：

```nginx
location /v1/ {
    proxy_pass http://127.0.0.1:18795;
    proxy_request_buffering off;
    client_max_body_size 64m;      # 不小于 --max-package-mib
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

加上 `--trusted-proxy 127.0.0.0/8`（或反代所在网段），限速和 sidecar 的 `client`
地址才按真实发送方计，否则经反代的所有上传共用一个桶。`GET /healthz` 返回
`{"status": "ok"}` 供运维探活；客户端探测的是其 base URL 下的 `/v1/hello`。用非特权账号运行，并把 `--save-dir` 当作敏感材料的
目录：包只做了凭据脱敏，没有匿名化。

### 告诉客户端往哪发 { #telling-clients-where-to-send }

`report-bug`、浏览器里的 *Report a bug* 和 `raven trajectory upload-bug-report`
读同一个设置：

| `RAVEN_BUGREPORT_URL` | 效果 |
| --- | --- |
| 未设置 | 用构建内置的默认值（本仓库为空：包留在本机） |
| 设为空值 | 关闭上传，即使构建内置了默认地址 |
| `https://ops.example.com/raven-bugreports` | 上传到那里；可以带路径前缀，客户端自己拼 `/v1/bugreports`；URL 里带查询串、片段或凭据会被拒绝 |
| `http://127.0.0.1:18795` | 明文 HTTP 只在本机被接受 |
| `http://10.1.2.3:18795` | 除非 `RAVEN_BUGREPORT_ALLOW_HTTP=1` 声明网络上的明文可接受，否则拒绝 |

URL 配置错误会让 `report-bug` 在采集任何东西之前停下。配置就绪后，确认页会显示
`Will upload to ...`，包一落地就发送，回执写入记录。上传失败时包保留、记录写明
原因，用下面的命令重传：

```bash
raven trajectory upload-bug-report REPORT_ID
```

配置了 URL 时，`report-bug` 与浏览器里的 *Report a bug* 会在采集任何东西之前先请求
接收器的 `/v1/hello`。没有应答时先打印原因，然后：

- 有 TTY 且未给 `--yes`：询问是否继续（默认否；选否则什么都不采集、不 pin）；
- 给了 `--yes`（有没有 TTY 都一样）：说明正在继续并照常进行；之后若上传失败，包会
  保留，可用 `upload-bug-report` 重传；
- 没有 TTY 且未给 `--yes`：按既有规则先以 `--yes is required without a TTY` 停下，
  不探测也不打包。

浏览器里的 *Report a bug* 运行在 TTY 上，同样询问；按 Esc 等同于否。WebUI 的
*Report a problem* 在向导打开时检查，见
[导出与问题报告](webui.md#export-and-report-a-problem)。`upload-bug-report` 不探测：
上传本身就会说明出了什么问题。

### 接收器校验什么 { #what-the-receiver-checks }

存入之前，包的大小、gzip 与 tar 结构、成员名与类型、清单哈希，以及
`needs_review`/`risk_accepted` 不变量都以流式方式校验，不解包任何内容。接收器
不扫描内容里的秘密：脱敏仍是发送方的责任，确认页上展示的就是外发的。

## 回放 Harness，而不重复动作 { #replay-the-harness-without-repeating-actions }

```bash
raven trajectory replay ATTEMPT_ID --strict
raven trajectory replay ATTEMPT_ID --warn --json --out /tmp/raven-replay-report.json
```

这里的 ID 定位已经保存的 bundle，也可直接传目录。Replay 在临时 workspace 中，
将录制的模型回复和工具结果交给当前 harness，不执行真实工具代码，也不让新模型重新评估任务。
回放一次部署记录，不会通过其中录制的工具调用再次部署。由外部代理（Claude Code、Codex 等）承担的子代理同样不会重新运行：
启动它的工具直接拿到录制的结果，该代理在自己运行中走过的步骤不进入回放。

| 模式/结果 | 含义 |
| --- | --- |
| `--strict` | 第一次请求与录制发生 divergence 时停止 |
| `--warn`，CLI 默认 | 记录差异，继续按顺序喂入录制 |
| Exit 0 | 到达结束；warn 模式仍可能存在 divergence |
| Exit 1 | 目标或调用错误 |
| Exit 2 | 严格差异或录制耗尽导致提前停止 |

查看 divergence 的调用种类/index、字段、预期值和实际值。
“无差异”说明当前 harness 重现了被比较的录制，不说明原任务正确。
修复后出现一个特定差异，可能正是所需证据。

## 制作回归用例 { #make-a-regression-case }

在源码树中，从已保存 bundle 或审核报告创建骨架：

```bash
raven trajectory regression init /path/to/bundle --name tool_result_preserved
```

命令最小化并脱敏 cassette，检查剩余 findings，全部 gate 通过后才在
`tests/trajectories/tool_result_preserved/` 发布草稿。已有目录会被拒绝。
完成以下文件后，才能认为用例就绪：

- `case.yaml`：issue、owner、保护的契约、什么情况下允许重录；
  对残留 findings 给出明确审核理由。
- `expect.yaml`：期望忠实复现还是特定的首次 divergence，并对真实请求作断言。

如果某个 fixture 的第一次模型请求现在包含修正后的用户消息，
以下**说明性 expectation** 可检查这个变化：

```yaml
mode: strict
divergence:
  kind: llm
  index: 0
  field: messages[1]
checks:
  - call: llm
    index: 0
    message: 1
    op: contains
    value: expected corrected text
```

Index、字段和文本必须来自自己的 replay report，不能原样粘贴到无关案例。
需要忠实复现时省略 `divergence`，并断言应保持稳定的行为。

```bash
raven trajectory regression validate tests/trajectories/tool_result_preserved
raven trajectory regression validate --all
uv run pytest tests/test_trajectory_regressions.py -q
```

静态校验检查元数据、cassette 完整性、残留审核和大小；Pytest 才运行 replay 断言。
校验成功不能替代实际测试。贡献 cassette 前读 `tests/trajectories/README.md`；
原始报告和报告资产不能放进仓库。

## 明确 Replay 不测试什么 { #know-what-replay-does-not-test }

当前比较覆盖模型/streaming 路径、消息 role 和非 system 内容、工具调用与工具名称；
有意不比较 system-message 正文和完整工具 schema。

录制媒体不会重新输入，工具 display/abort/blocks 元数据不完整重建，
并行交错按顺序消费。顺序变化会表现为 divergence，不会智能重配。
缺少 attempt 之前的历史也可能导致提前出现差异。

System prompt 装配应使用针对性单元测试；provider 行为、浏览器、并发和外部副作用
应使用真实集成测试。固定回复不能证明新模型仍选择相同动作。
[Evolver](evolver.md)执行新的基准评估，replay 则针对录制诊断 harness 行为。

## 排障与保留 { #troubleshooting-and-retention }

| 现象 | 检查 |
| --- | --- |
| 列表中没有 attempt | Trace root、追踪配置和日志是否保留 |
| 缺 session/artifact | Agent workspace 与 bundle manifest 完整性 |
| 回放立即停止 | 首次差异、缺失输入或 attempt 前历史 |
| Warn 成功退出但仍有 bug | 检查 divergence 并编写断言 |
| Init 后静态校验失败 | 完成草稿元数据/expectation，审核残留 |
| Trace 存储持续增长 | Pin 的语料与导出 bundle，不只是日志轮转 |

`raven trajectory unpin ATTEMPT_ID` 取消保留保护，不会删除导出的副本，
也不会给既有 bundle 脱敏。证据保留、分享和删除是不同决策。

实现位于 `raven/trajectory/replay.py`、`regression.py`、`cassette.py`、
`redact.py`、`package_check.py`、`receiver.py`、`upload.py` 和
`raven/cli/trajectory_commands.py`。
