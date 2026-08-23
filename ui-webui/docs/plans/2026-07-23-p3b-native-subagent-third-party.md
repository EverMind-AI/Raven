# P3b 实施计划 —— Raven 原生 subagent 的第三方 agent 调用(需求 5)

> 目标见 `docs/MIGRATION.md` §1 需求 5、§9.2、§9.3(d)。领导已确认:**落点在 `raven/agent/subagent/`,核心改 `manager.py`**。
> 一句话:把 `_run_subagent_inner` 里写死的 in-process raven loop 抽成**可切换后端**,新增 CLI(claude code / codex)与 OpenAI-API(mirothinker)两个第三方后端;`spawn()` 的后台并发结构不动,第三方调用因此天然并行(方案 C 的"原生 spawn 并行"路径)。

---

## 进展(2026-07-23,分支 `feat/gateway-web-channel`)

**核心已实现 + 单测(120 相关测试全绿,ruff 干净)**:
- **step 1**(commit `6f569c7`):抽出 `RavenLoopBackend` + `SubagentBackend` 协议,`manager` 按 backend 委托,行为零变化。
- **step 3–6**(commit `f41215f`):
  - config:`subagents.third_party`(判别联合 `kind: cli|openai`)在根 `Config`。
  - backends:`CliAgentBackend`(shell 调 claude/codex;`{prompt}` 单 argv token / `{prompt_file}` / stdin;host 运行、v1 无状态)+ `OpenAIApiBackend`(mirothinker;aiohttp 一次 Chat Completions;v1 无状态)。
  - `SubagentManager`:按 config 建 `name->backend` 注册表 + `_resolve_backend`(未知/None→raven loop)+ `list_third_party_agents`;`spawn()` 加 `agent`。**并发/回注结构不变** → 第三方天然后台并发。
  - `SpawnTool`:可选 `agent` enum + 动态 description 列出已配置 agent。
  - 串起 config→`AgentLoop(third_party_subagents=)`→`SubagentManager`;gateway 传 `ec_config.subagents.third_party`。
- 测试 `tests/test_subagent_third_party.py`:CLI(stdin/`{prompt}`/`{prompt_file}`/非零退出/超时)、OpenAI(本地 stub server)、manager 后端解析 + 坏条目跳过、spawn tool 参数 + 转发。

**待办**:
- **内置 preset**(claude_code / codex / mirothinker):现只有 config 结构,用户需自己在 `~/.raven/config.json` 的 `subagents.third_party` 填;可加一组开箱 preset(小工作量)。
- **真·端到端**:主 agent 实际 spawn 一个第三方(需 host 上装 claude/codex + 模型愿意调 spawn);单测已覆盖各后端 + 装配,真跑留待有环境时。
- **有状态 / resume 实例**:v1 无状态(每次 spawn 全新);与 P3 的实例注册表共享,后续再做。

## 0. 验收标准

1. 主 agent 能 spawn 一个第三方 agent(如 `claude_code`)执行任务,结果经 `Origin.SUBAGENT` 回注、主 agent 看到并汇报;
2. 不指定第三方时,行为与现在**完全一致**(默认 raven loop,现有测试全绿);
3. 一次发起多个第三方 spawn,后台**并发**执行(受 `Semaphore(max_concurrent)` 限流,默认 4);
4. `import agentscope` 不出现在任何新代码里(§9 判定)。

**冒烟**:配好 `claude_code` 后端 → 让主 agent "spawn 两个子 agent 分别做 A、B" → 两个 claude 进程并发跑 → 各自结果回注。

---

## 1. 设计:后端(执行器)抽象

### 1.1 协议
新增 `raven/agent/subagent/backends/`:

```python
# backends/base.py
class SubagentBackend(Protocol):
    async def run(self, task: str, *, task_id: str, workspace: Path, executor: Any) -> str:
        """执行一个子 agent 任务,返回最终文本结果。异常上抛,由 manager 统一 announce 为 error。"""
```

> 返回 `str` 与现有 `_announce_result`(`manager.py:240`)对接零改动 —— 它已负责 `wrap_untrusted` 围栏 + `Origin.SUBAGENT` 回注。

### 1.2 三个后端
| 后端 | 内容 | 来源 |
|---|---|---|
| `RavenLoopBackend` | 现 `_run_subagent_inner`(`manager.py:146-235`)的 raven-loop 逻辑**平移**进来(建 `ToolRegistry` + `provider.chat_with_retry` 循环)。默认后端,行为不变。 | 平移现有 |
| `CliAgentBackend` | shell 调 claude code / codex:文件式 prompt 投递 + id 策略(`provisioned`/`derived`)+ transcript 解析(正则 / codex jsonl)。 | 移植 `ref:_tool.py` / `ref:_transcript.py`(去 AgentScope 外壳) |
| `OpenAIApiBackend` | httpx POST `/chat/completions`,读原始 JSON 取回复。 | 移植 `ref:_openai_tool.py` |

### 1.3 `_run_subagent_inner` 改法
```python
async def _run_subagent_inner(self, task_id, task, label, origin, executor):
    backend = self._resolve_backend(origin.get("agent"))   # None -> RavenLoopBackend
    result = await backend.run(task, task_id=task_id, workspace=self.workspace, executor=executor)
    await self._announce_result(task_id, label, task, result, origin, "ok")
```
(异常路径与现有一致 —— 外层 `_run_subagent` 已 try/except announce error。)

### 1.4 并发结构不动
`spawn()`(`manager.py:74`)、`Semaphore(max_concurrent)`(`:67,:137`)、`_announce_result`(`:279`)全部保持。第三方后端跑在同一 fire-and-forget + 信号量结构里 → 自动后台并发。

---

## 2. 工具层 + 配置

### 2.1 `SpawnTool`(`raven/agent/tools/spawn.py:25`)
- `parameters` 增可选 `agent`(字符串,第三方 agent 名;缺省 = raven loop)。
- `description` 动态列出已配置的第三方 agent(名 + 简介),提示主 agent 何时点名派发。
- `execute(task, label, agent=None)` → `manager.spawn(..., agent=agent)`;`spawn` 把 `agent` 塞进 `origin` 传给 `_run_subagent_inner`。

### 2.2 第三方 agent 配置来源
**方案(建议)**:在 raven 配置里新增节(`raven/config/schema.py`),而非复用旧 dispatch registry:
```
[subagents.third_party.claude_code]  type="cli"  command=...  resume_command=...  id_source="provisioned"  transcript_format="text"  timeout=600
[subagents.third_party.codex]        type="cli"  ... transcript_format="codex_jsonl" id_source="derived"
[subagents.third_party.mirothinker]  type="openai"  base_url=...  model=...  api_key_env=...  timeout=1200
```
- 提供三个内置 preset(claude_code / codex / mirothinker),字段照搬 RavenX `_presets.py`(命令模板、正则、jsonl 标志)。
- `SubagentManager` 启动时读配置,构一个 `{name -> SubagentBackend}` 注册表;`_resolve_backend(name)` 查表。

> v1 先只做**无状态一次性调用**(每次 spawn = 全新会话)。有状态实例 / resume(RavenX 的 instance registry)留作后续,且与 §9.1 DAG 的实例注册表共享 —— 不在 P3b v1 范围。

---

## 3. 文件改动清单

**新增**
- `raven/agent/subagent/backends/__init__.py`
- `raven/agent/subagent/backends/base.py` —— `SubagentBackend` 协议
- `raven/agent/subagent/backends/raven_loop.py` —— 平移现有 raven-loop
- `raven/agent/subagent/backends/cli_agent.py` —— CLI 适配器
- `raven/agent/subagent/backends/openai_api.py` —— OpenAI-API 适配器
- `raven/agent/subagent/_transcript.py` —— codex jsonl 解析 + 正则(移植 `ref:_transcript.py`)
- `raven/agent/subagent/presets.py` —— 三个内置第三方 agent preset

**修改**
- `raven/agent/subagent/manager.py` —— `_run_subagent_inner` 选后端;`spawn()` 收 `agent`;`__init__` 建后端注册表;`_build_subagent_prompt` 归 `RavenLoopBackend`
- `raven/agent/subagent/__init__.py` —— re-export 新类型
- `raven/agent/tools/spawn.py` —— `agent` 入参 + 动态 description
- `raven/config/schema.py` —— `subagents.third_party` 配置节 + preset 装载

---

## 4. 分步实施(每步独立可验)

1. **纯重构:抽 `RavenLoopBackend`**。把 `_run_subagent_inner` + `_build_subagent_prompt` 原样搬进 `backends/raven_loop.py`,`manager` 委托它。**行为零变化**。验:`uv run pytest`(现有 subagent 相关测试全绿)。
2. **后端协议 + 选择**。`base.py` + `manager._resolve_backend`(缺省→raven)。验:默认路径不变。
3. **配置 + preset**。`schema.py` 加节 + 三 preset;`manager` 构注册表。验:`raven` 加载配置正常;注册表按配置生成。
4. **`CliAgentBackend` + `_transcript`**。文件式投递 + id 策略 + 解析。单测:喂造好的 claude/codex 输出,断言取到回复 + session id。
5. **`OpenAIApiBackend`**。单测:mock httpx,断言取到回复。
6. **`SpawnTool` 接线**。`agent` 入参 + description。单测:带 `agent="claude_code"` 走 CLI 后端。
7. **集成 + 并发**。主 agent spawn 第三方;两个并发 spawn 断言并发执行(信号量放行 2)。

---

## 5. 需拍板 / 风险

- **第三方 CLI 跑在哪**:claude/codex 需要 host 上的认证与 PATH。v1 **在 host 直接 shell**(照现 `dispatch-agent` 插件的做法),不进沙箱 executor;沙箱化留待后续。→ 需确认可接受。
- **配置来源(已定 2026-07-23)**:新增 raven 原生配置节 `subagents.third_party`(不沿用 `dispatch_agents.json`)。
- **有状态/resume 不在 v1**:原生 spawn 是一次性任务模型;instance/resume 与 DAG 共享,后续再做。
- **untrusted 围栏**:第三方结果经现有 `_announce_result` 的 `wrap_untrusted` 回注,天然被围栏。
- **超时/迭代**:`max_iterations=15` 是 raven-loop 专属;第三方后端各自用 `timeout`。

---

## 6. 开工前准备

- **分支**:`raven` 仓库现在在 `main`(且是 xuedizhan 的仓库)。动源码前需切工作分支 —— **分支名 / base 待与用户确认**(不直接落 `main`)。
- **可编辑安装**:`uv tool install --editable /Evermind/sh_evermind/xuedizhan/Raven --force`(否则改源码不生效)。
- **测试基线**:先跑一遍现有 `uv run pytest`(subagent 相关)存基线,重构后对比。
- **与 P1 的关系**:P3b 是纯内核工作,**不依赖 P1/P2**,可独立推进;两者可并行或择一先做。
