# 记录与支撑它的证据

一条记录，是关于某件事的一项持久化认知——一家公司、一个人、Raven 产出的一份文件、
它发出的一条消息——并且与支撑这项认知的证据放在一起。正是它让第二次遇到同一家公司的
运行能够找到第一次的记录，而不是再造一条；也正是这样，潜在客户才不会被两次告知
「从来没有人联系过您」。

本页描述 `records/` 库。当前检出提供该库、记录存储、状态机、`records.*` RPC 方法与
`raven records` 命令；尚未提供 WebUI 中的记录面板。
以下均为经过验证的行为。

## 一条记录持有什么 { #what-a-record-holds }

| 部分 | 是什么 | 为什么分开 |
| --- | --- | --- |
| `type` | `company`、`contact`、`artifact`、`interaction` 或 `opportunity` | 公司与人可能同名，但不会因此变成同一行 |
| `dedupe_key` | 记录所指的**那件事**的身份 | 存储对一件事只保留一行，因此重复写入是合并，而不是第二行 |
| `fields` | 记录相信什么 | 主张，与支撑它的证据分开 |
| `provenance` | 每个字段凭什么这么说 | 改错一个字段时，不必丢弃其余正确的部分 |
| `state` | 这件事走到了哪一步 | 两次运行必须对它达成一致，否则存储就不再是「发生过什么」的记录 |

没有 provenance 的字段，是记录无法辩护的猜测，`Record.unsourced_fields()` 会把它们点名。

## 合并，而不是成倍增加 { #merging-not-multiplying }

两次写入同一件事不是错误。存储会合并：

- 两次读数的**来源**都保留，各自仍连着它来自哪里；
- 较新一次读到的**字段**替换它读到的那些；
- **状态只向前走**——第二次运行发现某个潜在客户已合格时，不会把他退回去。

让合并成立的那道保证，是 `(type, dedupe_key)` 上的唯一索引。没有它，第二次运行会写出
第二行，两行随即互相矛盾。

## 状态链 { #the-state-chains }

记录的状态沿着一条链移动，而这条链是一连串**保证**，而不是一列标签。状态可以走到
下一步，也可以走到该链的结局；不能跳过某一步，也不能往回走。

| 种类 | 链 | 结局 |
| --- | --- | --- |
| `opportunity` | new、discovered、qualified、researched、artifact_built、pitched、engaged、replied、followed_up | won、lost——链上任意位置都可到达，因为一次运行可能提前结束 |
| `company` | new、discovered、qualified、researched | —— |
| `contact` | new、discovered、verified | —— |
| `artifact` | new、built、qa_passed | delivered——只能从 `qa_passed` 到达 |
| `interaction` | new、prepared、sent | delivered、bounced、unsubscribed——只能从 `sent` 到达 |

产物在通过质检之前不能交付，消息在发出之前不能投递。这两条最值得拥有，也正是
这些链有序而非自由的原因。

每一次移动都写进记录旁的日志：何时、从何处、到何处、为何。

## RPC 接口面 { #the-rpc-surface }

五个 JSON-RPC 方法把存储暴露给 harness 或工具：

| 方法 | 做什么 |
| --- | --- |
| `records.get` | 按 id 取一条记录 |
| `records.upsert` | 写入一条记录，合并进已有的那一条 |
| `records.list` | 按种类与状态列出记录 |
| `records.transition` | 沿链移动一条记录 |
| `records.grants.set` | 记录，或撤回，某来源的条款为何可在某条记录上留存 |

拒绝会带着自己的代码与自己的理由。查无此记录是 `record_not_found`（-32032），
因为「从未听说过这条记录」与「这条记录是空的」是不同的答案；在两者之间猜测的
客户端会把同一个潜在客户问两次。链不允许的移动是 `record_transition_refused`
（-32033），因为被拒绝的移动是信息，而不是需要绕开的失败。来源条款不允许留存的
字段是 `source_policy_refused`（-32034），且这份拒绝会点名记录与来源，于是那份
接受可以被写下，记录可以被重新写入。

## 命令行 { #the-command-line }

`raven records` 是操作者进入其中的方式：

| 命令 | 做什么 |
| --- | --- |
| `raven records list` | 按种类与状态列出记录 |
| `raven records get <id>` | 查看一条记录及其背后的证据 |
| `raven records grant <id> <source> --basis "..."` | 记录为何某来源的条款可在某条记录上留存 |
| `raven records revoke-grant <id> <source>` | 撤回那份接受 |

接受正是存储所索要的决策。当某个来源的条款禁止留存其数据时，存储会拒绝这次写入，
并在拒绝中**点名这条命令**，于是操作者被告知该运行什么，而不只是出了什么事。

## 存储放在哪里 { #where-the-store-lives }

每个 agent home 一个 SQLite 文件，位于 `<home>/records/records.db`，旁边是
`records.db.lock` 作为写锁。这个锁是仓库自己的跨平台建议锁，而不是直接用
`fcntl`：`fcntl` 在 Windows 上会退化为**未加锁**，那里的并发写会被丢弃而不是被串行化。

```python
from raven.records import Provenance, Record, RecordType, records_store

store = records_store()
store.put(
    Record(
        type=RecordType.COMPANY,
        dedupe_key="acme",
        fields={"name": "Acme", "employees": 120},
        provenance=[
            Provenance(
                field="name",
                source_url="https://www.sec.gov/edgar/full-text-search",
                source="sec_edgar",
                method="fetch",
            )
        ],
    )
)
```

## 什么可以被留存 { #what-may-be-kept }

一条记录只保留来源允许这套部署保留的东西。当某个来源的条款禁止留存其数据时，
存储会拒绝这次写入——而决定另行处置的部署，会先把该决定针对具体记录记录下来。
参见[数据来源及其条款](sources.md)。
