# 追踪 API

**raven**（以及任何其他接入方）与仓库内的 `raven.tracing` 实现之间的契约。
`raven-tracing` 是计划中的独立发行包，不是当前 Raven 包的独立依赖。

原则：*标准由 tracing 拥有，应用负责接入。* `raven.tracing` 定义什么是 span、
每种 span 携带哪些字段、以及如何渲染。应用（raven）通过在自己选定的位置调用一个
小而稳定的门面——`trace.span(...)`——来完成自我埋点。两侧都不依赖对方的内部实现，
唯一的耦合是这份 API 的版本号，以及下文的语义约定。

这与 OpenTelemetry 的模型一致（库定义 API 与数据模型，应用做手工埋点），因此同一套
纪律同样适用：API 保持微小且缓慢演进，其背后的 SDK（存储、查看器、磁盘格式）可以自由
迭代而不触碰接入方。

相关：磁盘记录的形态（`audit.span.v1`）定义在 `raven/tracing/spans.py`（`build_span`），
并在 §2 中做了摘要；本文是产生这些记录的**写入侧**标准。

## 1. 公开 API { #1-public-api }

一次导入，一个主调用：

```python
from raven.tracing import trace

with trace.span("llm.call", {"llm.provider": provider, "llm.model": model}) as s:
    resp = do_call(...)
    s.set({"llm.usage.total_tokens": resp.usage.total, "llm.finish_reason": resp.finish_reason})
```

### `trace.span(name, attributes=None, *, kind=None, **kw) -> Span` { #tracespanname-attributesnone-kindnone-kw-span }

一个上下文管理器：进入时开启 span，退出时定版并记录。

- `name`——点分的语义名称，`<domain>.<verb>`（例如 `llm.call`）。它决定默认的 `kind`，
  以及查看器中的标签与渲染方式（见 §2）。
- `attributes`——以全限定点分键构成的映射（标准形式，例如
  `{"llm.provider": p, "llm.model": m}`）。标准键都是点分的，所以映射是主要形式；
  `**kw` 接受裸键以便书写（原样存储，不做自动命名空间化——属性的命名空间可以与名称的
  域不同，例如 `session.turn` 携带的是 `turn.*`）。
- `kind`——可选，用于覆盖粗粒度分类（`session|model|tool|subagent|skill|memory|plugin`）。
  默认由名称的域推导；自定义节点请显式传入（§3）。

嵌套通过 `contextvars` 自动完成：在另一个 span 活跃期间开启的 span 会成为它的子节点；
上下文可跨 `await` 存续，并会快照到任务上。一轮对话的根节点是 `session.turn` span，
其余一切都嵌套在它之下。

### `Span` 句柄 { #span-handle }

| 方法 | 作用 |
|---|---|
| `s.set(attributes=None, **kw)` | 将属性合并到该 span（点分键映射和/或裸关键字参数） |
| `s.artifact(key, payload, *, kind="json")` | 将大体积负载落盘到行外存储，并挂上 `<key>.artifact_path/_sha1/_bytes` 与一段截断的 `preview`。用于提示词、工具输入输出、召回结果。 |
| `_spans.address_items(items)` | 把一组消息按内容寻址存放到 `audit-artifacts/_messages/` 下，返回可嵌入负载的 `{"$msg": sha1}` 引用。参见下文的 `audit.artifact.v2`。 |
| `s.event(name)` | 追加一条时间线事件 `{time, name}` |
| `s.error(exc)` | 标记 `status = ERROR`（若代码块抛出异常则自动完成） |

只读属性：`s.trace_id`、`s.span_id`、`s.name`。

### 模块级辅助函数 { #module-helpers }

| 调用 | 返回 |
|---|---|
| `trace.enabled()` | 记录是否开启（来自配置或环境变量） |
| `trace.current()` | 当前活跃的 `TraceCtx`，否则为 `None` |
| `trace.use_context(ctx)` | 上下文管理器：重新进入由 `current()` 捕获的上下文，用于在调度该工作的那一轮之外开启的 span（例如由长驻工作进程消费的队列） |

### 硬性保证（接入方为何是安全的） { #hard-guarantees-why-an-adopter-is-safe }

1. **关闭时为空操作。** 追踪被禁用时，`trace.span(...)` 返回空操作句柄：
   不写入 span 或产物，`with` 代码块照常执行。
2. **异常处理边界。** span 创建、span 写出和产物持久化会捕获内部异常并以 debug 级别记录日志。
   `with` 代码块内抛出的异常会在 span 被标记为错误后重新抛出。
   句柄方法仍要求输入类型正确：例如 `s.set(42)` 会抛出 `TypeError`，并传播到代码块外。
   请通过 `s.set({...})` 或 `s.set(attributes={...})` 传入映射；
   `attrs` 不是参数别名，会被存储为普通的属性键。
3. **导入安全。** 即使没有任何配置存在，导入 `raven.tracing` 并调用该 API 也必须成功。

### `@trace.instrument(...)`——装饰器（接入方的主要机制） { #traceinstrument-the-decorator-primary-adopter-mechanism }

接入方通过给方法加注解来埋点——方法体不受影响，因此这不会改变核心逻辑（只增加一层
观测包装）：

```python
from raven.observability import semconv

@trace.instrument("llm.call", extract=semconv.llm_call)
async def chat_with_retry(self, ...): ...
```

`trace.instrument(name, *, kind=None, seed=None, on_open=None, extract=None)`
可以包装同步**或**异步方法：

- `extract(span, bound_args, result, exc)`——在 `finally` 中执行（出错时输入也已捕获），
  负责填充最终的属性与产物。`bound_args` 是按名称组织的调用参数；`result` 是返回值
  （出错时为 `None`）；`exc` 是抛出的异常（成功时为 `None`）。标准提取器位于
  `raven.observability.semconv`（`llm_call`、`tool_call`、`memory_*` 等）。
- `seed(bound_args) -> dict`——返回 `session_key` / `channel` / `chat_id`，用于开启一个
  *根* span（一轮对话），其身份会被所有子节点继承。
- `on_open(span, bound_args)`——在开启之后、方法体之前执行；用于记录输入，并对进行中的
  根节点调用 `span.checkpoint()` 以支持实时查看。

提取器会用到的额外 `Span` 方法：`span.retype(name, kind)`（一个后来发现其实是
`skill.read` 的 `tool.call`）、`span.cancel()`（丢弃一个条件 span，例如 `skill.inject`
只在确实注入了内容时才保留）、`span.checkpoint()`、`span.elapsed_ms()`。对于不应成为
活跃父节点的叶子标记，请传入 `detached=True`——可取消的 span 必须如此，否则在取消之前
开启的子节点会悬挂在一个未发出的 span 之下。

每一个 span 家族都以这种方式埋点——包括 `subagent.run`：子代理运行的是同一批被装饰的
原语（`chat_with_retry` / `tools.execute`），因此它的 span 由那些装饰器捕获，并借助
`asyncio.create_task` 在派生时取得的 contextvars 快照，自动嵌套在 `subagent.run` 节点
之下。全程没有使用任何 monkeypatch。

## 2. 语义约定（标准 span 类别） { #2-semantic-conventions-standard-span-kinds }

`kind` 是单词形式的分类（决定节点配色与分组）。`name` 是 `<domain>.<verb>` 标识符
（决定标签与渲染）。属性按域划分命名空间。接入方**应当**填充「必填」列；「可选」列
能带来更丰富的渲染效果。

| name | kind | 必填 | 可选属性 |
|---|---|---|---|
| `session.turn` | `session` | — | `turn.input_preview`、`turn.output_preview`、`turn.in_progress`、`turn.capabilities.{tools,plugins,skills}` |
| `llm.call` | `model` | `llm.provider`、`llm.model` | `llm.provider_class`、`llm.finish_reason`、`llm.call_id`、`llm.invocation_source`、`llm.usage.{input,output,total,cache_read,cache_write}_tokens`、`llm.usage.cost_total`、`llm.request_{bytes,images,image_bytes}`、`llm.http_status`、`llm.served_by`、`llm.response_id`；产物 `llm.input`（`audit.artifact.v2`：消息引用与工具，另含 `request` = 消息加工具的负载大小与图片数量，而非服务商最终请求体，以及 `generation` = 本次调用向模型提出的要求；见下文）、`llm.output`（另含 `call` = 传输记录） |
| `tool.call` | `tool` | `tool.name` | `tool.call_id`、`tool.duration_ms`、`tool.error`；产物 `tool.input`（参数）、`tool.output`（结果） |
| `subagent.run` / `subagent.call` | `subagent` | — | `subagent.id`、`subagent.label`、`subagent.task`、`subagent.session_id`、`subagent.parent_trace_id`、`subagent.parent_span_id`、`subagent.trace_id`、`subagent.status` |
| `skill.read` / `skill.inject` | `skill` | — | `skill.name`、`skill.id`、`skill.source`、`skill.path`、`skill.scripts_dir`（存在即表示物化了一个可运行的 bundle，而非仅有说明文本）、`skill.read.via_tool`（`use_skill`/`read_skill`/`read_file`）、`skill.inject.{names,count,via}` |
| `memory.recall` / `.store` / `.feedback` / `.extract` / `.consolidate` / `.profile_refresh` | `memory` | — | `memory.scope`、`memory.hits`、`memory.message_count`、`memory.kind`、`memory.deposit_summary`、`memory.deposit_status`、`memory.surface`、`memory.sections_rewritten`；产物按操作类型各异 |
| `plugin.load` / `tracing.bootstrap` | `plugin` | — | `plugin.name`、`plugin.contribution`、`plugin.id` |

### 产物格式 { #artifact-formats }

一个产物要么是普通的 JSON 负载（v1，没有判别字段），要么是 `audit.artifact.v2`——后者
携带 `"artifactFormat": "audit.artifact.v2"`，并把每条消息替换为指向
`audit-artifacts/_messages/<sha1[:2]>/<sha1>.json` 的 `{"$msg": "<sha1>"}` 引用。
只有 `llm.input` 以这种方式写入；其余类别一律保持 v1，且磁盘上已有的 v1 产物永不重写。
外壳中的 `request` 与 `generation` 原样携带：两者都不含消息内容，改为引用只会多一层
间接寻址而毫无收益。

`raven/tracing/artifact_v2.py` 是该格式的唯一权威——规范序列化、外壳结构、引用校验、
引用解析都在其中——因为随附的查看器用 JavaScript 镜像了同一套逻辑，并有一个跨语言的
往返测试比对两者。解析外壳的消费方必须遵守两条规则：`messages` 解析为消息对象列表，
而 `systemPrompt` 与 `prompt` 解析为其消息的 `content` 并强制转为文本。sha1 不是 40 位
小写十六进制字符的引用属于数据而非地址，原样透传；外壳也可能在无法写出 blob 时内联
携带一条原始消息。

对 `llm.input` 而言，`artifact_sha1` 与 `artifact_bytes` 描述的是外壳，而不是它所引用的
对话；`llm.request_bytes` 才是请求体量的信号，它直接从消息本身测得，因而不受外壳影响。
外壳对其列出的引用求哈希，因此篡改痕迹可定位到单条消息，而不只是证明负载被改过。

有两处解析点会把外壳还原为等价的 v1：打包轨迹时的 `raven/trajectory/bundle.py`
（使 bundle 不依赖消息存储），以及随附查看器 `server.js` 中的 `readArtifact`
（使 UI 永远看不到引用）。位于二者下游的消费方——回放、Trajectory Cassette 的生成、查看器 UI——
读到的都是 v1 形态，无需了解 v2。

**服务商标注：** `llm.provider` 是调用路由到的*逻辑后端*（例如 `openrouter`），由模型的
网关前缀推导；`llm.provider_class` 是当两者不同时的具体实现类（例如 `LiteLLMProvider`）。
`llm.served_by` 是第三样东西：*响应*自称由谁提供服务，仅在上游明确给出时才存在——在一个
会扇出的网关背后，这是判断究竟是谁作答的唯一途径。

**传输记录。** `llm.output` 的 `call` 对象记录的是这次交换做了什么，而不是模型说了什么：
`http_status`、`served_by`、`served_model`、`response_id`、`headers` 和 `body`——最后一项
仅在调用什么也没返回时才填充，因此可用的回答绝不会被存两遍。它只由
`raven.providers.call_record` 构造，该模块会限制 body 与 headers 的体量，并替换任何以
凭据命名的 header 的值；其他任何地方都不得构造传输记录。请求自身的字节永远不会被复制进
记录：`llm.input` 的 `request` 对象只做计数（传输格式下的 `bytes`、`images`、解码后的
`imageBytes`），图片负载只被计数，不被记录。

命名规则：
- `name` = `<domain>.<verb>`，小写点分。
- 属性键 = `<domain>.<field>`，与该 span 的域一致。
- kind 是封闭词表：`session|model|tool|subagent|skill|memory|plugin`。

## 3. 自定义节点 { #3-custom-nodes }

任何接入方（或插件）都可以记录自定义 span，无需注册：

```python
with trace.span("raven.sentinel.tick", {"sentinel.reason": r}, kind="plugin") as s:
    s.set({"sentinel.fired": n})
```

规则：
- `name` 请使用**自有命名空间**（`raven.<subsystem>.<verb>`），以免与 §2 的标准名称冲突。
- 显式传入 `kind`（否则会退化为通用节点类别）。
- 查看器对未知名称采用通用渲染（标题取自 `name`，副标题取自选定的某个属性）。若需要定制
  渲染，请提供一条**描述符**条目（`descriptors/*.json`，以 `name` 为键）——这是查看器的
  渲染标准，随附于 `raven/tracing/viewer/descriptors/`。

## 4. 接入方集成契约（raven） { #4-adopter-integration-contract-raven }

1. 追踪功能以 `raven.tracing` 的形式随 Raven 分发，无需独立的 `raven-tracing` 依赖
   或 `raven[tracing]` 可选组。
2. 在埋点处 `from raven.tracing import trace`，用 `with trace.span(...)` 包裹目标操作。
   埋点位于应用自身的代码中，随重构一同移动，并在 diff 中可见（没有外部 monkeypatch
   会悄悄失效）。
3. 通过 `[tracing].enabled`（raven 配置）或 `RAVEN_TRACING=0`（环境变量覆盖）来开关。
   禁用时该 API 为空操作。
4. 应用永远不导入 SDK 内部实现（存储、查看器）——只使用门面。

不存在 monkeypatch 或自动埋点路径：全部埋点都是 raven 自身源码中显式的
`@trace.instrument` 注解，因此它随代码移动、在 diff 中可见（不会在重构时悄悄失效）。

## 5. 版本与治理 { #5-versioning-governance }

下列 API 版本规则是针对独立发行包的提案；当前实现仍随 Raven 一同发布，尚无独立包版本。

- API 与语义约定作为 `standard-api.v1` 一同版本化，独立于应用。
- **新增性**变更（新的可选属性、新的 span 名称或类别）→ 次版本号递增，向后兼容。
- **破坏性**变更（重命名或删除属性、改动 API 签名）→ 主版本号递增，并附迁移说明；
  接入方应锁定受支持的版本范围，并在不匹配时发出警告（而不是静默降级）。
- 应通过一致性快照测试（冻结的 span 名称与必填字段）在 CI 中守护独立包的契约，
  并要求契约变更时同步提升版本号。
- 磁盘记录格式单独版本化为 `audit.span.v1`（定义在 `raven/tracing/spans.py`）；
  两者各自独立演进。

## 现状 { #status }

仓库内实现，已完整。每一个 span 家族（turn / llm / tool / memory / skill.inject /
plugin.load / subagent）都通过 raven 自身方法上的 `@trace.instrument` 完成埋点；没有
monkeypatch，也没有 `instrument.install()`——自动探测模块已被移除。
Raven 专用的属性与产物构造器已位于 `raven.observability.semconv`，与追踪机制分离。

留待独立开源阶段（P4）处理，均不阻塞仓库内使用：
- 让导入变为可选：raven 核心在模块加载时硬导入 `raven.tracing`（装饰器在类定义时应用），
  因此它必须随 raven 分发；要让追踪成为真正可选的 extra，需要先有一个空操作回退垫片。
- 将 Raven 专用的提取器保留在 `raven.observability.semconv` 中；独立发行包只包含
  通用 API、schema 与查看器。
- 将 Raven 专用的默认值与路径解析（`FRAMEWORK`、`RAVEN_*` 环境变量、
  `~/.raven` 路径）与独立包解耦。
- 冻结 `standard-api.v1`；发布 `raven-tracing`；raven 默认依赖它。
