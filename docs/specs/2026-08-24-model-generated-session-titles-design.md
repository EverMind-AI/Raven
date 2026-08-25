# 模型生成会话标题 — 设计稿

状态:已实现,待评审。基线 `origin/main`(GitLab),分支 `feat/model_generated_session_titles`。

## 1. 目标

1. **后端**:新会话开始时,单独调一次模型生成短标题,写入 `metadata["title"]`。
2. **GUI**:消息发出后标题位先用生成动画占住,标题返回时填入。
3. **TUI**:`resume` 时展示 title。

不在范围内:重命名/重生成 UI、fork 继承真值表的行为变更、`ui-webui`(它有独立的 session 模型,连机械标题都没有)。

## 2. 现状锚点

| 关注点 | 位置 | 现状 |
|---|---|---|
| 标题真源 | `raven/session/manager.py:561`(`save()` 内) | 未命名会话取首条 user 消息首行、折叠空白、截 40 字符,打 `title_auto: True` |
| 派生函数 | `manager.py:57` `_derive_title` / `:70` `_first_user_auto_title` | 纯字符串操作,不碰 provider |
| 人工标题 | `manager.py:123` `set_title` | 清掉 `title_auto`,即"人工"标记 |
| fork 继承 | `manager.py:674` | `title_auto` 为真的标题不继承 |
| RPC | `raven/rpc/methods/session.py:630` `session.title` | 只有 get/set,**没有服务端主动推的通道** |
| 事件通道 | `raven/rpc/methods/turn.py:181` | `emitter.emit(session_key, {"type", "payload"})`,会话作用域 |
| 事件类型 | `raven/rpc/models.py:317` 起 | `Literal[...]`,新增须同步 regen `ui/src/rpc/generated.ts` 与 `ui-tui/src/rpc/generated.ts`(签入且有门禁) |
| GUI 标题写入 | `ui/src/live/050-turn.js:281` `titleFromFirstMessage` | 发送瞬间用首行截 30 字符,并 `rpc.call('session.title')` |
| GUI 事件分发 | `ui/src/live/050-turn.js:55` 起 | 单一 `ev.type ===` 链 |
| GUI 骨架屏原语 | `ui/src/styles/page.css:2737` `.skel .sk` + `@keyframes skshine` | 已带 `prefers-reduced-motion` 保护;`ui/src/features/rail/RailPage.tsx:257` 已支持整表 skel |
| TUI resume | `session.resume` → `info: SessionInitInfo`(`models.py:2011`)→ `introMsg` → `SessionPanel`(`ui-tui/src/components/branding.tsx:218`) | `info` 里**没有** title 字段 |
| 侧路 LLM 先例 | `raven/evolver/judge/llm_client.py:115` | `chat_with_retry` + 可选 model override,继承空响应重试 |
| 短标题 prompt 先例 | `raven/proactive_engine/sentinel/predictor/prompts.py:39` | tool-schema 约束 `title` ≤30 字符、祈使句、无尾标点,prompt 刻意短以便便宜模型稳定出 JSON |
| 配置先例 | `raven/config/raven.py:497` `BehaviorsExtractConfig` | `enabled` + `model: str | None`(None 继承上级) |

## 3. 设计决策

### D1 触发时机与输入:首条 user 消息落地即触发,只喂 user 消息

不等 assistant 回复。三条理由:GUI 的占位动画必须尽快收敛,等回复会让动画挂 10s+;标题命名的是**用户的诉求**,不是模型的回答;首条消息拿到即可发起,与本轮推理并行。

代价与闸门:纯问候("在吗")会生成低信息标题。首条消息 strip 后短于 `min_input_chars` 直接走机械截取,不花这次调用。

与现有兜底的关系:`save()` 的机械标题**照旧先写**,保证任何时刻标题非空;模型标题回来后覆盖。

### D2 标记:沿用 `title_auto`,不新增字段

模型标题写入时照样打 `title_auto: True`,含义不变——"机器起的,不是人打的"。`set_title()` 清它([manager.py:131](../../raven/session/manager.py))、fork 继承读它([manager.py:674](../../raven/session/manager.py))两处逻辑一个字不动,**fork 行为零变化**。

早先草稿里提过再加一个 `title_source` 三态字段区分"机械截取/模型生成/人工",已否决:防覆盖靠 `title_auto` 缺失即可判定人工;模型调用只在首条消息触发一次,不存在幂等问题;UI 也没有标注标题来源的需求。多一个持久化字段就多一份存量会话的兼容判断,收益为零。

### D3 覆盖竞态:写回前重读,人工标题一律不覆盖

LLM 调用在飞期间,用户可能 `/title` 手改,或第二个客户端改了同一会话。写回前重读 metadata:`title_auto` 已被清(即有人手改过)→ **丢弃模型结果,不发事件**。

懒会话(尚未落盘)复用 `session.title` set 的既有契约:留内存、`pending=True`、随首次 save 落盘。

### D4 模型与失败:失败即静默退回,已在位的机械标题就是兜底

新 config 段 `session_title`,形状抄 `BehaviorsExtractConfig`:

```
enabled: bool = True          # 已定:默认开
model: str | None = None      # None 继承主模型
timeout_seconds: float = 8.0
budget: int = 24              # 模型生成预算(码点),见 D6
min_input_chars: int = 8
```

超时/空返回/超长/解析失败 → 不改标题、不发事件、不向用户报错,日志 debug 级。调用走 `chat_with_retry`(承 `llm_client.py:115` 的先例),输出用 tool-schema 约束(承 `prompts.py:39` 的先例)。标题语言跟随用户首条消息的语言,prompt 里给中英双示例,不强制英文。

### D5 事件:新增会话作用域事件 `session.title`

payload `{session_id, title}`,走 `emitter.emit(session_key, ...)` 而非广播——一个会话的标题不该点亮别人的侧栏。需在 `rpc/models.py` 加事件模型并 regen 两份 `generated.ts`(签入产物,改源要照 Makefile 的 `lint-tui` 单子跑,别凭记忆)。

### D6 长度上限:生成预算与展示截断是两件事

实测一批真实模型标题(11 条):码点 2-21,中位 10。模型自然就写这么短,所以后端的数值闸**在实践中基本不触发**,它不是排版规则,是跑飞护栏。展示宽度归前端,且已经实现。

| 闸 | 值 | 施加点 | 越界处理 |
|---|---|---|---|
| 模型生成预算 | 24 码点 | prompt 声明 + 服务端 clamp | 超 2x(48)/ 含换行 / 被引号包裹 / 带 "标题:" 前缀 → 判定未遵循指令,**丢弃,保留机械标题**,不发事件 |
| 机械截取 | **40 码点,不动** | `_derive_title` | 行为不变 |
| 存储硬顶 | 200 码点 | `set_title` 单一入口 | **不静默截断**——拒绝并回报;换行与首尾空白折叠成单行 |
| 展示 | 各端自定,**已实现** | CSS / `stringWidth` | 省略号,永不回写存储 |

三条理由:

- **机械截取的 40 不收窄。** 早先草稿想统一到 24,理由是排版;既然排版归前端,40 与 24 对用户可见结果没有差别,而改它会变更所有渠道(CLI / 飞书 / Telegram)已有会话的截断位置。不动即零风险。
- **存储硬顶与排版无关。** 200 防的是人工改名把一整段粘进 metadata,是数据完整性闸。人打的标题**不静默截断**:悄悄砍掉后半截,用户下次看到一个残句且不知原因。
- **GUI 的 30 是删除,不是同步。** 新设计下 GUI 不再写标题(它现在写只为了别让侧栏挂着"新任务",而占位动画正是干这件事的),客户端常量整个消失。超时兜底也不写:后端 `save()` 早已落好机械标题,重读一次 `session.title` 即可。

**连带必修:** GUI 改名路径 `DS.sessions.renamed`([080-overrides.js:334](../../ui/src/live/080-overrides.js))是 `.catch(() => {})`,吞掉一切失败。现在无上限所以无所谓,一旦 set 会拒绝就变成"改名看起来生效、刷新后回滚"。同文件 `DS.sessions.pin` 上方那段注释("A refused persist must not stay quiet")讲的正是这个,改名照抄该处理。

## 4. 三端 UI

### GUI

改 `titleFromFirstMessage`(`live/050-turn.js:281`):不再立刻写首行标题,而是把该会话置为 pending,渲染 `.sk` shimmer 条(复用 `page.css:2737` 的原语,reduced-motion 已被照顾)。收到 `session.title` 事件填入并清 pending。

三条硬约束:

- **动画必须有终点。** 超时(比后端 `timeout_seconds` 略长,建议 12s)或事件丢失 → 落回首行截取。永久停在动画上是比没有动画更糟的失败态。
- **两处都要改。** 侧栏 `.sess` 行和顶栏 `#title` 现在由同一函数写,占位与填入都得覆盖。
- **占位条宽度用固定值(建议 40%),不用随机。** `RailPage.tsx:266` 的骨架行用的是确定式 `52 + (i*17)%30`;标题只有一条,宽度抖动会在填入时让整行跳动。

### TUI

`SessionInitInfo`(`models.py:2011`)加 `title: str | None`,`session.resume` 天然带回。`SessionPanel`(`branding.tsx:218`)在 panel 顶部单独一行展示标题,`sid` 留在 `footerMeta`——标题是身份,sid 是定位,两者生命周期不同,不该挤进同一行(与 `manager.py:428` 那段注释同一个道理)。

TUI 不做生成动画:新会话标题生成时用户正在看 agent 输出,标题不在视线内。

`session.titled` 事件在 TUI 侧是显式 no-op([chatStream.ts](../../ui-tui/src/app/chatStream.ts) 的穷尽 switch 必须处置每个新事件变体)。理由不是事件不重要,而是终端没有可落的位置:这个会话的 panel 早已被生成它的那一轮输出顶到屏幕之上。标题已经落盘,下次 resume 由 `info.title` 读出来,什么都没丢。

## 5. 测试

按 §5.1 落进现有文件,不新建:

- `tests/test_session_manager.py` — 模型标题打 `title_auto` 后 fork 继承行为不变(现有用例在 `:889`)
- `tests/test_rpc_session.py` — 竞态丢弃、懒会话 pending、事件是会话作用域
- GUI:`ui/src/features/rail/RailPage.test.tsx` 一侧加 pending 行渲染;超时落回需要独立用例

变异检验必须造出**能失败**的用例,尤其这三条:(a) 人工标题在飞时模型结果被丢弃;(b) 超时后 GUI 落回机械标题而非停在动画;(c) 事件不广播到别的会话。三条都是"不做也看不出来"的行为,断言写松了等于给空转测试盖章。

## 6. 待定项

无。`enabled` 默认开、生成预算 24 码点、机械截取维持 40、展示交前端,均已定。
