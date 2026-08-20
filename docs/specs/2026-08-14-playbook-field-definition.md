# Playbook 字段定义

日期：2026-08-14
状态：已定稿，实现对齐中（本文标注了本期不予支持的字段）
相关：`2026-08-11-external-agent-registry-design.md`（`nodes[].agent` 解析到的那张表）

一个 playbook = 一个目录，目录里一个 `playbook.md`，分三区。

```
~/.raven/playbooks/
  competitor-scan/
    playbook.md           # 一个目录一个文件，没有旁挂
```

```markdown
---
name / description                     # 身份信封
---

正文：给人读的说明书，机器不解析，两种 mode 一致

```yaml playbook-spec
全部机器字段
```
```

**判别靠路径，不靠字段**：在 `playbooks/` 扫描根下就是 playbook。不复用 skill 的
`SKILL.md`，所以不需要 `metadata: '{"raven": {"playbook": true}}'` 这类标记——同一件事
有两个судья，就会出现"放在 skills 下却声明是 playbook""放在 playbooks 下却漏了声明"
两种矛盾，而这两条规则本不需要存在。

**块在正文后而不是全塞 frontmatter**：理由是阅读顺序。正文说明书通常十几行，机器字段带上
`promptTemplate` 往往上百行，短的在前长的在后读得顺。

命名 camelCase。

---

## 1. frontmatter

两个字段，不可再加。放这里是为了索引器只读文件头就能建列表，不必解析全文。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 | 全局唯一 id，= 目录名，`^[a-z0-9][a-z0-9-]*$` |
| `description` | string | 是 | 一句话意图，写"什么时候该用我"，≤200 字 |

---

## 2. 块 · 顶层字段

| 字段 | 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| `version` | integer | 默认 `1` | 格式版本，非内容版本 |
| `mode` | enum | 必填 | 图从哪来：`dag` 图写死在 `nodes`；`prompt` 图由模型按 `prompts` 当场组装 |
| `confirm` | boolean | 默认 `true` | 整体跑前是否要用户确认。**只管被动命中**：命中后经 ask_user 问一句（确认往返只暂停 turn，不需要独立状态机），答否/超时回落普通对话；gate 难分高下时改为让用户从候选里挑（挑中即确认）。显式入口（`run_playbook` 工具、CLI `run`）本身就是用户意志，不再问。没有问答通道的环境（无 broker）按未设防直接派发，防线退回 L2 门 |
| `triggers` | object | 必填 | 见 §3 |
| `params` | map | 可选 | 运行时入参，见 §4 |
| `nodes` | list | dag 必填 ≥1 / prompt 禁止 | 见 §5 |
| `prompts` | string | prompt 必填 / dag 禁止 | 组图指导：告诉模型怎么摆出这张图——用哪些 agent、分几层、谁依赖谁 |

`mode` 与 `nodes` / `prompts` 双向校验，违反即加载失败。

**两种 mode 只在"图从哪来"这一步不同，拿到图之后完全同一条链路**：过校验（§8）→ 确认闸 →
后台异步执行 → 回执 run_id → 完成后回注。所以 `confirm`、`instance` 规则、占位符对两种 mode
一致生效，不设执行形态字段。

`prompt` 模式给出来的图必须符合 §5 的 `nodes[]` 结构，跟之前这套同一套校验，
给不出合法图就报错而不是硬跑。

**没有旁挂文件，也没有状态字段**：一个目录一个 `playbook.md`，装的全是"这个流程是什么"。
生成器的假设与未答问题写进正文（`## Open questions` 小节）给人审，不进机器字段；用户原话不留存。

**开不开是配置，不是字段**。playbook.md 是分发单元——发给同事、传上 Hub 都是它；
"我这台机器上把它关了"是本机状态，跟着文件走就错了。所以开关住
`config.json` 的 `playbooks.disabled` 名单里：不在名单上就是开着，`disable` 往里加、
`enable` 往外拿。这样内置和用户自建的**没有任何区别**——同一条命令、同一个名单，
内置只是不能删（没有目录可删），关是能关的。

---

## 3. triggers

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `keywords` | list[string] | 是，≥1 | 归一化后子串匹配（小写、全半角折叠、空白压缩）；中文不分词。长词短语都放这里 |

命中 `keywords` 是一级初筛（零成本），命中后过 LLM 门控判别加抽参。

**两种 mode 走同一条发现路径。** 不按 `mode` 分流——否则用户笔里同样是 playbook，
一种会被自动触发、一种只能靠检索找到，这是能被直接感知的行为差异。

护栏（`triggers.py`）对**每一条**填 L1 索引的路径生效，包括模型直出的词表：
一个泛词的代价是"每条含它的消息都要付一次门控调用，且永久存在"。

---

## 4. params.&lt;键&gt;

声明每次运行时可变的入参。`promptTemplate` / `prompts` 里用 `${params.<键>}` 引用。

| 字段 | 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| `type` | enum | 默认 `string` | `string` / `integer` / `number` / `boolean` / `enum` / `path` |
| `required` | boolean | 默认 `false` | 为真且抽不到值时向用户追问 |
| `default` | any | 可选 | 有默认值则永不追问 |
| `enum` | list | `type: enum` 时必填非空 | 取值表。门控做选择题，抽准率高于填空 |
| `description` | string | 必填 | 参数说明，同时用作缺参追问的话术 |

三个消费方：门控抽参的目标 schema、缺参追问、编译期替换 `${params.x}`。

`type: path` 编译期过 `check_confined`，不许逃逸出会话工作目录。

没有可变入参的 playbook（如"每周拉一次 issue 列表"）整段省略。

**缺参追问是无状态的**：匹配不留状态，用户的答复会从头重新进 L1。所以追问话术里必须带上
能重新命中的示范（触发词 + 槽位），否则一句"2026-08-11"提名不到任何 playbook，这次运行
就丢了，而用户以为自己答了。

---

## 5. nodes[]

| 字段 | 类型 | 必填/默认 | 说明 |
|---|---|---|---|
| `id` | string | 必填 | `^[A-Za-z0-9_-]+$`，图内唯一，会成为产物文件名 |
| `agent` | string | 必填 | agent 注册表里的 name。接入方式（cli / acp / 进程内）、密钥、能力参数都在注册表，playbook 不重复声明 |
| `promptTemplate` | string | 必填 | 本步任务书，占位符见 §6 |
| `dependsOn` | list[string] | 默认 `[]` | 依赖的节点 id；同时是引用白名单 |
| `skills` | list[string] | 可选 | 本步注入的 skills |
| `mcps` | list[string] | 可选 | 本步注入的 mcp |
| `instance` | string | 可选 | 会话句柄，同句柄的节点共享一个 agent 会话 |
| `confirm` | boolean | 默认 `false` | 节点级闸：本步执行前单独要用户确认 |

配置挂在节点而不是角色上，因为同一个 agent 可以在一张图里跑多步、每步任务不同：

```yaml
nodes:
  - {id: a1, agent: research-raven, dependsOn: [],   skills: [市场调研]}
  - {id: b,  agent: code-raven,     dependsOn: [a1]}
  - {id: a2, agent: research-raven, dependsOn: [b],  skills: [代码审计], mcps: [github]}
```

`confirm` 两级的分工：顶层管"这个 playbook 要不要跑"，节点级管"这一步不可逆动作要不要放行"。
发布、发信、付款这类节点应该写 `confirm: true`——顶层的一次确认盖不住流程中段。

### 5.1 本期不予支持的字段

以下字段在契约里成立，但**当前实现无法兑现，因此在加载校验期直接拒绝**而不是接受后降级：

| 字段 | 状态 | 为什么拒绝而不是降级 |
|---|---|---|
| `confirm`（节点级） | ~~校验失败~~ → **字段已删除** | 闸的意义是拦住不可逆动作，而"跑完再提示"是负安全。但一个唯一用途是被拒绝的字段不该存在：闸收到图级（`PlaybookSpec.confirm`，注入 dag 的图级 `confirm`），审的时候看到的就是整张图。见 [2026-08-19-unified-agent-registry-design.md](2026-08-19-unified-agent-registry-design.md) 的 D6/D13 |
| `instance` | ~~校验失败~~ → **可用** | 这一行的理由（"内置 agent 无 resume 机制"）与代码不符：raven 自己重放 message list（`instance_state.py`），内置 agent 恒 stateful。统一注册表后规则 7 按表判定，句柄真的生效；本期的限制改成"同句柄的非链头节点不得再写 skills/mcps"（resume 沿用首次的 system prompt） |
| `mcps` | 接受 + 降级提示 | 少一个工具是能力损失不是正确性问题，节点照样完成自己那步。为它整图失败不划算 |

区别在于**安全闸失败即拒，能力缺口降级告知**。

---

## 6. 占位符

| 语法 | 时刻 | 出现在哪 | 含义 |
|---|---|---|---|
| `${params.x}` | 编译期 | `promptTemplate` / `prompts` | 参数值 |
| `{{ dep.output }}` | 运行期 | `promptTemplate` | 依赖节点的输出全文 |
| `{{ dep.output_path }}` | 运行期 | `promptTemplate` | 依赖节点输出的文件路径 |
| `{{ ref:路径 }}` | 运行期 | `promptTemplate` | 工作区文件内容 |
| `{{ ref_path:路径 }}` | 运行期 | `promptTemplate` | 工作区文件路径 |

编译期只替换 `${…}`，`{{…}}` 原样透传给运行期。

两种 mode 都用这些：`prompt` 模式先把 `${params.x}` 替换进 `prompts` 交给模型，
模型给出的图里照样带 `{{…}}`，由 runner 在运行时解析。

`output` 与 `output_path` 的选择：本地 agent 传路径（自己去读，大产物不占上下文），
远端 API agent 只能传内容。注册表里 `readsLocalFiles` 为否的 agent 用 `output_path`
会被能力预检挡下。

---

## 7. 图语言

`dependsOn` 是全部的图语言，并行隐式——没有依赖关系就并行，不需要 parallel 语法。

| 写法 | 含义 |
|---|---|
| `dependsOn: []` 或省略 | 图的起点，立即开跑 |
| `dependsOn: [a]` | 等 a 完成 |
| `dependsOn: [a, b]` | 等 a 和 b 都完成，汇合 |
| 两个节点同一个下游 | 彼此无依赖，并行，再汇合 |

`dependsOn` 同时是引用授权：`{{ x.output }}` 里的 `x` 必须在本节点的 `dependsOn` 里，
否则编译期报错（default-deny）。

**表达不了的三件事**：环（条件回退，如"审校不通过重写"）、条件跳过（无 `when:`）、
per-playbook 并发上限（由 runner 全局配置管）。

注意 `mode: prompt` **不能**绕过这一条。它是图的生成器，不是运行时编排器——图组装一次
就固定，跑到一半不能根据中间结果改图。所以"连续两轮无新增发现就停"这类循环，两种 mode
都写不到，`prompts` 里只能写"怎么摆这张图"，不能写"跑起来之后怎么判断"。

---

## 8. 校验规则

加载时全跑，不过就进隔离区（不触发、不可运行、在列表里带错误信息展示）。

| # | 规则 |
|---|---|
| 1 | `mode: dag` → `nodes` 非空且无 `prompts`；`mode: prompt` → 有 `prompts` 且无 `nodes` |
| 2 | `frontmatter.name` = 目录名，且目录位于 `playbooks/` 扫描根下 |
| 3 | `nodes[].agent` 必须在注册表里，缺失即报"需要先注册 X"，不等到跑那一步才炸 |
| 4 | 图无环，且所有节点从起点可达 |
| 5 | `{{ x.output }}` 的 `x` 必须在本节点 `dependsOn` 内 |
| 6 | `${params.x}` 的 `x` 必须在 `params` 里声明 |
| 7 | `instance` 仅从注册表标 `stateful` 的 agent 可用（拿不到表时**不查**，不拿假清单判） |
| 8 | 同 `instance` 的节点之间必须存在依赖链——共享会话不能并发，会互相踩上下文 |
| 9 | 同 `instance` 的**非链头**节点不得写 `skills` / `mcps`——会话启动时菜单就定了，写在后面的那份读它的人以为生效、实际不生效。旧口径"必须一致"允许重复写三遍，正是这个误读 |
| 10 | `{{ dep.output_path }}` 仅当注册表标该 agent `readsLocalFiles` 时可用 |
| 11 | `mode: prompt` 给出的图，执行前过 4-10 全部规则；不合法则重组，仍不合法则报错，不降级硬跑 |

规则 11 是本设计的关键：`prompt` 模式没有逃生舱口。模型组出来的图和作者手写的图
走同一套校验、同一条执行链路。
