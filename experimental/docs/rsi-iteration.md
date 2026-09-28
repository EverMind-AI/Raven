# 迭代自我改进：Iteration 与 Analyst

Curator 一次生成的 Harness 不会一开始就完全对。本文说明 Harness 怎样在多轮交互里被持续改进：每轮让员工真实工作，评价者说出问题，Analyst 把话整理成要求，Curator 据此修订，下一轮再检验。Curator 本身的结构见 [design.md](design.md)。

## 1. 闭环

```mermaid
flowchart TD
    O[入职：任务说明与交出的资料] --> C0[Curator 首次生成 Harness]
    C0 --> T[Trial：员工真实工作，产出会话与交付物]
    T --> E[评价者：人、数据集或模拟老板]
    E -->|Signal：原话、逐项结果、是否满意| A[Analyst]
    T -->|会话、执行记录、机制活动| A
    P[上一轮 Plan 的预期] --> A
    A -->|curate：行为要求| C[Curator 修订 Harness]
    A -->|continue / supplement / clarify / stop| L[Iteration 决定下一步]
    C --> T
    L --> T
```

- 入职那次 curation 没有任何执行记录，Curator 只凭任务和资料生成机制。
- 之后每轮先执行、再评价、再修订。最后一轮只评价、不修订，因为没有下一轮能检验那次修订。
- 停止条件：Analyst 决定 stop；所有评价者都满意且不需要修订；或轮次用完。

## 2. 角色与信息边界

| 角色 | 看得到 | 看不到 | 产出 |
|---|---|---|---|
| Trial | 员工与对话方的往来 | 评价标准 | 本轮会话 |
| 评价者 | 对话、交付物、自己的标准 | 员工内部实现 | `Signal` |
| Analyst | 评价者的原话、会话、执行记录、机制活动、上一轮预期 | 评价者私下的打分与标准 | `Feedback`：决定与行为要求 |
| Curator | 行为要求、当前 Harness、材料、执行观测 | 评价标准、评价者原始打分 | 修订后的 Harness |

三条边界：

- **评价者不直接对 Curator 说话。** 人的一句话、数据集的分数、模拟老板的评审，都先经 Analyst。
- **Analyst 停在诊断层。** 它说明哪个行为没达到、证据是什么、和上一轮预期差在哪；不说改哪个策略面、怎样实现。机制归因是 Curator 的事，只有 Curator 能读源码和装配事实。
- **要求可观察、不写答案。** 每条要求写情境与期望行为、实际行为、证据、验收方式，不写"加一个 intake 清单"这类机制建议。

## 3. 一轮的调用时序

```mermaid
sequenceDiagram
    participant IT as iteration.run
    participant TR as Trial
    participant WK as Worker
    participant AN as Analyst
    participant EX as iteration.exchange
    participant CU as curator.workflow
    IT->>TR: run(worker)
    TR->>WK: 对话 / 执行任务
    WK-->>TR: 回复、交付物、observations.jsonl
    TR-->>IT: sessions
    IT->>AN: review(worker, sessions, previous_signals, previous_feedback, history)
    AN->>AN: 取得 Signal（评价者），附上机制活动 activity(sessions)
    AN->>EX: 一次有界的模型交换，可用 read_records 查询执行明细
    EX-->>AN: Feedback（decision + requirements + filtered）
    AN-->>IT: Review
    alt decision == curate 且不是最后一轮
        IT->>CU: improve(worker, provider, feedback)
        CU-->>IT: 新版本生效
    else 其他决定或最后一轮
        IT->>IT: 记录本轮，继续或停止
    end
```

每轮的会话、Signal、Feedback、curation 记录都按轮关联写进 worker 根目录的 `iteration/`、`analysis/`、`curation/`，读者可以随运行进度查看。

## 4. Analyst

输入是一组固定的通用字段：任务、本轮与上一轮的 Signal、会话、上一轮预期、上一份 Feedback、历史、技能与组合结构、节点要求、证据位置。执行明细不整段塞进去，由模型用 `read_records` 按需查询。

处理在一次模型交换里完成：

1. **分离**：一段话里可能同时有任务补充、行为评价和控制意图，分开处理。
2. **过滤**：运行故障、证据找不到的评价、已兑现的要求，不触发修订，并在记录里写明原因。
3. **归纳**：多条评价指向同一行为时合并，一条评价涉及多个行为时拆开；对照上一轮预期标注是新要求、承诺未兑现，还是已兑现但仍不满意。
4. **表达**：按 `Requirement` 写出行为、实际表现、证据、验收、强度与复发次数。

输出 `Feedback` 的决定有五种：`curate`、`continue`、`supplement`（缺材料）、`clarify`（需要澄清）、`stop`。

**机制活动。** Analyst 会把"装上的机制本轮实际做了什么"一并交给 Curator（`analyst/activity.py`）：哪条审核放行了几次、打回了几次、理由是什么，哪个工具调用失败了。Curator 据此核对自己上一轮的修订是否起效，而不是只看它的计划。

## 5. 一条反馈怎样变成执行层机制

以模拟场景的展示案例为例：老板在第 1 轮指出，方案 PPT 里写着"来源：调研第 2 节"这类内部备注，酒店名旁边没标"以计调确认为准"。

1. 这段话被整理成要求：客人拿到的方案里不能出现内部备注，出现酒店名就要标注；证据是第 1 轮方案第 4 页原文；验收是下一轮方案里没有这类字样。（这个案例用的是单层做法，老板的评审直接给出要求；现在默认由通用 Analyst 从老板原话整理。）
2. Curator 在 understand 阶段核对：当前只有 playbook 节点提示词在约束需求单内容，没有执行层检查。它在 select 阶段决定，在写需求单的子 Harness 上加一个 `action.strategy`：需求单写盘前检查内部字样和酒店标注，不合格就打回重写；同时给做 PPT 的子 Harness 加一个自检工具。
3. 第 2 轮，内部备注没有了，但出现新的问题：门票被概括成"一人免票、一人半价"。Curator 这次在同一个审核里加上"按每个孩子的年龄和调研原文逐个核对"的检查。
4. 第 3 轮，这项要求守住了；机制活动显示审核实际打回了 4 次需求单。

## 6. 场景怎样接入

闭环本身不认识任何场景。一个场景只需提供：

| 通用接口 | 场景提供 |
|---|---|
| `Trial.run(worker) -> Sessions` | 谁和员工对话（真人、模拟客人、数据集样例） |
| 评价者或 `Analyst` 子类 | 谁来评价、怎样说话 |
| 任务说明与材料 | 员工的岗位说明，以及何时交出哪些资料 |

真人入口是 `python -m experimental.iteration`：真人既是对话方也是评价者。旅行社模拟在 `experimental/simulation/`，见上一级 [README](../README.md)。
