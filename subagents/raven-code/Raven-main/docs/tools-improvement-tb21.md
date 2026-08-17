# Raven Tools 改进方案与落地记录（TB2.1 归因）

> 数据来源：Terminal-Bench 2.1 全量 run（qwen3.6-35B-A3B）
> `agent-eval-data/runs/terminalbench/qwen3.6-35B-A3B/{raven/tb21-raven-full, terminus/tb21-terminus-full}`
> 对照材料：codex / opencode / x-agent 的工具设计分析与源码。
> 分析日期：2026-07-31；同日完成四个落地 commit（见 §4）与 6 题复验 run（见 §3）。
> 提分数字均为机制层面估计，非实测；复验结果为实测。

---

## 0. 基线与总体诊断

| | pass@1 | 通过任务数 |
|---|---|---|
| raven | **39.3%** | 35/89 |
| terminus 2 | **44.9%** | 40/89 |

- raven 挂 / terminus 过：14 个；terminus 挂 / raven 过：9 个（含 `qemu-alpine-ssh`——raven 的 `exec_write`/`exec_read` 交互能力是真实优势，8 次 write + 68 次 read 驱动 QEMU 串口）；双挂 40 个（编译器、密码学、MIPS、path-tracing 等硬题）。
- 失败结局：54 个失败中 **40 个以"声称完成"收尾**（假完成 74%）；~5 个中途死掉无最终回复；~11 个长任务 max input ≈229k 逼近 262k 窗口且全部失败。

工具调用与错误统计（全 89 任务）：

| 工具 | 调用 | 错误 | 备注 |
|---|---|---|---|
| exec | 2814 | 124 | 主执行面 |
| edit_file | 514 | 17 | fallback 匹配 + diff 提示有效 |
| read_file | 441 | 3 | |
| write_file | 389 | 0 | |
| exec_read | 238 | 18 | 错误集中且模型无法恢复（P0-4） |
| grep | 109 | 0 | |
| web_search | 17 | **17** | 100% 失败：Serper key 未配置（P0-5） |
| job_status/job_wait/job_cancel | 0 | — | 该 run 尚无 detached job；引入后路由是关键（P0-1） |

---

## 1. 对症下药（P0）——全部已落地

### P0-1 `background+session` 让"要留下的服务器"随 agent 一起死 ✅

TB run 中所有 `background:true` 都落在 PTY session 里（随 agent 退出被杀）。四个任务的服务器在 verifier 到场前死掉：pypi-server（pip install connection broken）、configure-git-webserver（HTTP 000）、kv-store-grpc、qemu-startup。四个任务里 agent 运行期都验证过服务是活的再声称完成。terminus 全过这几题只因为 tmux 里 `cmd &` 的进程活在容器里。

关键细节：detached `BackgroundJobRegistry` 引入后，初版路由是 `background and not session → job`，而**这 4 个任务的 6 次调用全部带了 session 名**（给后台进程命名是模型本能）——初版一个都救不回。已改为 `background:true` 无条件走 detached job，session 名用作 job 名；description/TOOLS.md 同步写明生命周期。

### P0-2 安全 deny 正则误伤 ✅

`\b(shutdown|reboot)\b` 匹配 QEMU 的 `-no-shutdown/-no-reboot`（install-windows-3.11 被拦 4 次后失败）；`\bmkfs\b` 连 `which mkfs.ext4` 都拦。已改命令头锚定（`(?:^|[;&|]\s*|\bsudo\s+)`），只拦"被调用"；报错带命中的 pattern 与片段。评测 harness 侧另设 `denyPatterns: []`。

### P0-3 `timeout ≤ 600` 硬拒绝 ✅

tune-mjcf 连续 5 次提交 timeout 610/660 被 schema 拒绝。已改 clamp + 指引（"for longer work use background:true + job_wait"）；上限配置化 `tools.exec.maxTimeout`；评测 harness 设 3600。

### P0-4 session 错误不可恢复 + `exec_read`/`read_file` 混淆 ✅（即时部分）

compile-compcert 一题里 10 次 `exec_read` 不存在的 session + 6 次把 `exec_read` 当 `read_file` 用，16 轮无效。已落地：session 未命中错误教创建路径；registry 通用 did-you-mean（参数指纹完整匹配另一工具时直接点名）。中期 unified exec 见附录 A（暂缓）。

### P0-5 `web_search` 注册了但 100% 不可用 ✅

17/17 调用失败"Serper API key not configured"。已改为有 key 才注册（与 media tools 的 opt-in 一致），无 key log 一行原因。评测环境若要吃到收益需配真实可用的 search key。

---

## 2. General 改进（源自 codex / opencode / x 对照）

raven 已经做对的（不动）：head+tail 截断并报告总量；edit_file fallback + 最相似片段 diff；grep/find 系统根遍历拒绝；detached job 的设计本身；工具结果 UNTRUSTED 包裹。

### G-1 错误即提示体系化 ✅

- 不明确的工具名（`Read_File`/`execRead`）做无歧义归一化修复并直接执行（学 opencode V1 repairToolCall），带 note 说明；
- 统一的 `[Analyze the error above...]` 后缀从"所有错误"收敛到"只有超时和意外异常"——校验错误已有 did-you-mean、工具自带错误自己负责指引，叠加通用后缀是噪声；
- 文件工具 not-found 错误点名恢复工具（list_dir/find 定位、write_file 新建）。

**反转风险 ≈0**：只改失败路径文案，成功路径零字节变化。

### G-2 大输出落盘 + 回读路径 ✅

exec/session 输出超 30k chars 全量落盘 `~/.raven/tool-output/`，截断标记给路径 + grep/read_file 指引；job 日志截断标记带 log 路径；home 不可写退回旧文案。

### G-3 apply_patch ⛔ 不做（调研结论）

opencode 的门控：`modelID.includes("gpt-") && !oss && !gpt-4` 时启用 apply_patch **且移除 edit/write**（两套编辑面互斥）。apply_patch 是 OpenAI 自家 freeform 格式（GPT-5 系按此训练），qwen/claude 系按 str_replace 训练。对 qwen 系预期负收益；将来服务 GPT 系模型时按 opencode 的互斥方式引入。

### G-4 工具面 manifest 记录 ✅

注册完成与 MCP 接入后各 log 一行：工具数 + schema sha256 指纹 + 名单（进 stderr.log 被 harness 收录）——分数波动可归因到工具面变化。

### G-5 coding 系统提示 profile ✅

`agents.defaults.profile: assistant | coding`。默认 assistant 字节不变；coding 完全效仿 opencode default prompt 的结构与内容（qwen 在 opencode 里吃到的就是这份：tone/conventions/no-comments/verify-with-tests + 完成前逐项核对 + `<env>` 块），**工具相关段全部改写为 raven 的工具面**（find/grep/read_file/edit_file/write_file 路由、服务器和超上限工作用 background job、交互程序用 session+exec_write/exec_read、UNTRUSTED 规则）。评测 harness 设 `profile: coding`。

**反转风险为五项中最大**（系统提示每题每轮生效；正向题的行为是在旧提示下形成的均衡），建议单独 A/B：10 正向 + 10 失败混合子集，专门盯正向保持率。

### G-6 同参熔断 + 历史配对修复 + 中途落盘 ✅

- **same-call breaker**：字节级相同的 (tool, args) 连续硬失败 3 次后，第 4 次不执行直接返回强指引。**限定 all-error 是硬约束**：35 个正向题里同参 streak≥3 有 13 处但全部是成功轮询（caffe 6、qemu-alpine-ssh 7），all-error 的 streak 为 0——照抄 opencode 的"不看成败一律熔断"会打断合法轮询，直接高危两个正向题；
- **历史配对修复**：加载历史中悬空的 tool_call 补合成 "[aborted]" 结果、孤儿 result 丢弃——崩溃不再污染后续 provider 调用；
- **中途落盘**：每 10 个迭代持久化一次 turn-so-far（TB 任务是单 turn 数百迭代，崩溃曾丢掉整条轨迹——上次 run 有 5 个任务连 session 文件都没有）。

---

## 3. 复验实测（tb21-raven-toolfix6，含 P0 修复，2026-07-31）

对上次 6 个相关任务重跑（配置与全量 run 逐字一致，仅 run_id/并发/任务集不同）：

| 任务 | 上次 | 本次 | 归因 |
|---|---|---|---|
| pypi-server | ❌ 服务器死于 agent 退出 | ✅ **1.0** | 模型重演同样的 `background+session` 组合，新路由转 detached job（`[job: pypiserver, pid 91]`），服务器活到验收 |
| configure-git-webserver | ❌ HTTP 000 | ✅ **1.0** | 同上 |
| kv-store-grpc | ❌ 服务器死 | ❌ 但 **5/7 tests**（上次服务器不在）；`test_real_grpc_server_running` 已过，余下失败是 proto 字段定义与隐藏测试不符（任务理解） |
| install-windows-3.11 | ❌ guard 拦 4 次 | ❌ 但 **guard 0 拦截**，QEMU 正常启动，verifier 1/4 → 2/4；剩余是 Win3.11 安装界面键击交互（terminus 也挂的双挂硬题） |
| qemu-startup | ❌ | ❌ 192 轮鏖战（无 KVM 启动极慢 + serial/telnet 语义），双挂硬题，非工具问题 |
| tune-mjcf | ✅（浪费 5 轮 timeout 拒绝） | （运行中） |

净变化：**+2 题翻绿**，2 题失败方式从"工具杀掉成果"变成"任务本身难"。错误恢复机制也拿到活证据：模型 `job_status` 查错 job 名时，错误里的 "Known: job1" 让它下一轮即恢复。

---

## 4. 落地 commit 清单（feat/exec_sessions_and_deny_config 分支）

| commit | 内容 |
|---|---|
| `40b1b83` feat(*): detached background jobs and exec tool reliability fixes | P0-1/2/3/4即时/5 + G-2 + G-4（含 detached job 基础设施） |
| `396fe3a` feat(agent): actionable tool errors and mangled tool name repair | G-1 |
| `19f37c4` feat(agent): same-call breaker, history pairing repair, mid-turn checkpoints | G-6 |
| `b8955ef` feat(context): coding system-prompt profile modeled on opencode | G-5 |

harness 侧（独立仓库）：AgentEval raven harness 设 `denyPatterns: []`、`maxTimeout: 3600`、`profile: coding`；TerminalEval raven harness 设 `maxTimeout`。

全部改动带测试（`uv run pytest` 相关文件绿；ruff lint/format 通过）。已知无关既有失败：`TestConnectMcpSandboxGuard::test_stdio_no_executor_does_not_raise`（改动前即失败）、channels 可选依赖缺失的 collection error。

### 4.1 复查后的第二批修复（2026-07-31，待提交）

对落地代码复查 + 正向轨迹触点扫描后补的五项（1b killpg 因在 caffe-cifar-10 等正向题中存在"孤儿下载进程继续跑完"的依赖，暂缓）：

| 项 | 问题 | 修复 |
|---|---|---|
| A | `DirectExecutor` 自带 `_MAX_TIMEOUT=600` 二次 clamp，静默吃掉 harness 的 `maxTimeout: 3600`（P0-3 未打通到真 executor；mock 测试没覆盖） | executor 层去掉 clamp，ExecTool 是唯一裁剪点；补真 executor 测试 |
| B | `exec_read`/`exec_write`（schema 上限 600）与 `job_wait`（上限 3600、默认 300）没设 `timeout_seconds`，registry 300s 默认兜底会先杀——与"长构建用 job_wait"的新指引直接冲突 | `_SessionTool.timeout_seconds=660`；`job_wait` schema 上限降为 1800、backstop 1860 |
| 1a | exec 超时仍返回 `stdout=""`，600s 构建被杀后零信息（上轮 53 次） | communicate() 改为持续 drain：超时保留已捕获输出，`ExecResult.timed_out` 结构化标记 + as_text 恢复指引；kill 语义不变（killpg 是缓做的 1b）。附带修复：`cmd &` 的孙进程占着管道不再把调用拖到自身退出 |
| 2 | plain exec 走 `/bin/sh`（Debian 系=dash），模型写的 bashism 报错或静默变义（上轮 30+ 次 `sh:` 报错）；session/job 路径已是 bash，唯独 plain exec 不是 | 有 `/bin/bash` 时用 bash，否则回退 sh；description 注明 |
| 3 | edit_file 多匹配只说 "appears N times" 不给位置，模型须重读全文件定位（上轮 29 次，overfull-hbox 133 次 edit） | 错误列出每处匹配的行号+首行内容（上限 8 条）；新增 `occurrence` 参数定点替换；`Warning:`→`Error:` 前缀统一（同参重试可被 same-call breaker 拦截）；空 old_text 指向 write_file |

新增/更新测试：`tests/test_filesystem_tools.py`（新）、`test_sandbox_unit.py`（超时保留输出、无 executor clamp、bash、后台孙进程不阻塞）、`test_tool_registry_timeout.py`（session/job 工具 backstop ≥ schema 上限）；`test_tool_registry_execute.py` 中一个仍断言通用后缀的旧测试改为断言 G-1 的新行为。

### 4.2 第三批：低风险 P1 清尾（2026-07-31，待提交）

| 项 | 问题 | 修复 |
|---|---|---|
| inherit_env | DirectExecutor 的 env 白名单把任务镜像 `ENV`（LD_LIBRARY_PATH/HF_*/JAVA_HOME）静默剥掉，容器内失败难归因 | `tools.exec.inheritEnv`（默认 false），DirectExecutor 全部三条路径（exec/session/job）生效；AgentEval + TerminalEval harness 置 true |
| read_file 预算 | 单次 128k chars（≈40k token）；无单行截断（minified 单行文件会把一次读爆掉，且有"首行超限返回空"的边界 bug） | 上限降 48k；单行 >2000 chars 截断并标注（对齐 opencode 三重上限）；保证至少返回一行 |
| write_file 信号 | 只报"写了 N 字节"，模型不知道是新建还是覆盖（circuit-fibsqrt 连写 4 版自己覆盖自己） | 回显 `Created new file` / `Overwrote existing file (was N bytes, now M bytes)` |
| web_fetch 格式 | 整个正文 `json.dumps`，换行/引号全转义，token 膨胀且难读回 | 纯文本：URL/Status/截断标注头 + `---` + 原文；错误改 `Error:` 前缀纯文本（顺带接入 registry 错误通道） |
| 行号前缀说明 | read_file 的 `N| ` 前缀偶尔被抄进 edit 的 old_text（opencode edit.txt 有对应条款） | read_file description 声明前缀非文件内容；edit_file description 声明 old_text 不得含前缀 |
| edit 成功快照 | 编辑成功零反馈，坏编辑到运行时才暴露 | 单点编辑成功后回显改动区 ±2 行的新内容（带行号，上限 12 行），学 opencode 的 post-edit 预览 |

测试：`test_filesystem_tools.py` 扩展（read 长行/预算/单行超大、write created/overwrote、edit 快照）、`test_sandbox_unit.py`（inherit_env 对照）、`test_security_web_ssrf.py` 改断言纯文本错误。

### 4.3 第四批：包裹分级 + read-before-edit（2026-07-31，待提交）

| 项 | 设计 | 先例 |
|---|---|---|
| UNTRUSTED 分级 | `tools.wrapToolOutputs: all\|external`（默认 all，产品零变化）。external 时只有 web_search/web_fetch/deep_research/mcp_* 的结果套完整 fence，本地工具结果原文进上下文；subagent 结果播报保持恒包。省 ~50 token/条，200 轮省 1.5–2 万 token | opencode 完全不包；codex 不包工具结果、AdditionalContext 分 Untrusted/Application 两级——按来源分级是共识，全量包裹没有先例 |
| read-before-edit | `FileReadTracker`（进程内 path→mtime_ns）。edit 一个本 session 没读过的文件 → 报错引导 read_file；读后被外部改过（sed -i 等）→ 报错引导重读。read_file/write_file/edit_file 成功都记为"已见" | opencode（edit 硬报错 + writeIfUnchanged CAS）与 Claude Code（强制先 Read + mtime 跟踪）均强制；codex 例外但 patch 上下文行等效 |

harness 侧：AgentEval + TerminalEval 均设 `wrapToolOutputs: "external"`。测试：`test_security_trust.py`（策略分级）、`test_filesystem_tools.py::TestReadBeforeEdit`（未读拒绝/读后放行/自写放行/连续编辑/外部修改要求重读/无 tracker 不强制）。

---

## 5. 暂缓项（附录）

### 附录 A：unified exec —— 把 session 从"执行的前提"变成"执行的结果"

codex Unified Exec 语义：所有命令跑在 PTY 里；yield 时间内结束 → 返回 output+exit_code；**没结束 → 不杀进程，返回 session_id**，之后 `write_stdin` 交互、空写入轮询。这消灭 "no session named X" 死角类（模型先跑 coqtop、后想交互，现设计要求它预知）。

暂缓理由（复杂度×风险）：
- 工程复杂度五项最高（PTY 生命周期、超时语义反转、与 session/job 三方交互，1-2 周起）；
- **反转风险有实测暴露面**：正向题里 plain-exec 超时被杀出现 8 次，恰好集中在 qemu-alpine-ssh 与 tune-mjcf 两个正向题——这些流程依赖"超时→杀死→换策略"的现有语义；挂住的进程还会吃 MAX_SESSIONS=8 名额（qemu-alpine-ssh 一题 8 次超时正好吃满）；
- P0-4 即时修复已把恢复成本从 10 轮降到 1-2 轮，增量收益变小。

若将来做：不做全局语义反转，用 opt-in 参数 `on_timeout: kill|detach`（默认 kill）增量交付。

### 附录 B：假完成治理 —— todo 工具 + "完成必须有验证证据"

40/54 失败以声称完成收尾，期望值最高（TB +2~4pt）但方差也最高。暂缓理由：正向题里 9 题 ≤10 轮（最短 4 轮），todo 对短题是纯开销；"todo 全绿才许完成"的硬约束对弱模型有收尾死循环风险；须与 profile 机制一起 A/B。

分步建议：
1. **可先行（零 raven 侵入）**：harness 的 continue prompt 改为 "Before replying TASK_COMPLETE, re-read the original task and verify each deliverable exists and passes its checks."——只在模型没说 TASK_COMPLETE 时出现，不碰正常流；G-5 的 coding 提示已内置同义纪律（"before declaring a task complete..."）；
2. todo 工具用 opencode 式全量替换语义（无 ID/无 diff/幂等），description 写死"仅多步任务使用"与"未验证不得标 done"；完成约束用软 nudge 不用硬阻断；
3. 长期：X 式 PostToolUse hook——连续多次编辑无任何 exec 验证时注入 nudge。

---

## 6. 核心方法论（三家共识，作为 raven tools 的验收标准）

1. **每次失败都必须是下一步的可执行指引**（opencode"错误即提示"、codex RespondToModel、x is_error+恢复建议）；
2. **工具让模型看到的必须是真实环境状态**：exit code、running/finished、谁还活着、什么被截断了；
3. **生命周期语义不能靠文档兜底**——模型不读 TOOLS.md，语义必须编码在 schema/路由/返回值里（P0-1 的教训）；
4. **不可用的能力不进 schema**；
5. **完成必须有验证证据**，工具（todo/plan/hook）为这条纪律提供抓手；
6. **熔断类机制必须先在正向轨迹上量化误伤面**（G-6 的 all-error 约束来自 13 处合法轮询 streak 的实测）。
