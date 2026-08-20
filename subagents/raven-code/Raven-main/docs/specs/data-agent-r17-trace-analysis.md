# R17 · 三题四方轨迹归因（PATENTS q2 / crmarenapro q12 / crmarenapro q2）

日期：2026-08-17。输入：我方 Opus 5 与 v4-flash(L21) 各 5 attempt 的完整会话轨迹与 `answer.txt`，
Permute Core（Claude Opus 5，tuned prompt，榜 0.8330）与 Sentinel（Fable-5，榜 0.8450）的公开轨迹，
以及 DAB checkout 中的 `query.json`。

**边界声明**：全程未读取 `ground_truth.csv` / `validate.py`；期望答案值来自评测输出的 verdict（评估结果是合法的分析输入）。
本文提出的修复候选一律为通用能力，每条附「是否含题目特定内容」自检。

**一句话结论**：三题里有两题（PATENTS q2、crmarenapro q12）的正确结果**在我们自己的轨迹里已经算出来了**，
失败发生在「选哪个 / 怎么写出去」这一步；第三题（crmarenapro q2）所需的判别方法我们也用过一次并因此通过，
但没有被强制触发。剩余差距的主体不是分析能力，是**决策与交付的强制化**。

---

## 1. PATENTS query2

### 1.1 题面与政策

```
Find the CPC technology areas in Germany with the highest exponential moving average of patent
filings each year (smoothing factor 0.1) for patents granted in the second half of 2019.
Include the full title, CPC group code, and the best year for each CPC group at level 4.
```

无附加政策段。题面里有两个互相牵制的短语：

- `the CPC technology areas ... with the highest EMA` —— 最高级，看起来在**筛组**；
- `the best year for each CPC group at level 4` —— `for each` + `best year`，说明最高级实际落在**每组内部选年份**，
  交付面是「level 4 的每一个 CPC 组，各给一行」。

### 1.2 四方答案与得分

| 方 | 得分 | 交付行数 | 形态 |
|---|---|---|---|
| Ours(v4-flash L21) | 0/5 | 5 次全部 2 行 | `TITLE - CODE - YEAR` / `CODE \| TITLE \| YEAR` 等 |
| Ours(Opus 5) | 1/5 | 4 次 2 行、1 次 23 行 | 唯一通过的 attempt_004 是那次 23 行 |
| Permute | 5/5 | 23 行 | markdown 表格，列 `full_title / cpc_group_code / best_year [/ ema]` |
| Sentinel | 5/5 | 23 行 | 方法论散文 + Python 元组列表 `[('BAKING; EDIBLE DOUGHS','A21',2015), ...]` |

验证 verdict 给出的拒绝理由（我方）：

```
Name fuzzy match failed for 'BAKING; EDIBLE DOUGHS' (best match: 'reasingermanywitht', distance=13)
```

`BAKING; EDIBLE DOUGHS` = A21，是我们**没有交付**的 21 个组之一；`reasingermanywitht` 是我方 attempt_001
散文抬头 `...technology areas in Germany with the highest...` 归一化（小写、去标点、去空格）后的滑窗片段。
即：校验器在整段答案文本上做归一化滑窗模糊匹配，找不到 `A21` 的名字，就退化匹配到抬头噪声。
**这不是格式问题，是覆盖问题。**

形态容忍度的反证：Permute 交付纯 markdown 表格 5/5 过，Sentinel 交付大段散文 + Python 元组字面量 5/5 过，
我方 attempt_004 交付 `TITLE | CODE | YEAR | EMA=x` 也过。**答案形态被广泛容忍，唯一决定成败的是行集是否完整。**

### 1.3 各方解读链对比

| 决策点 | Ours(flash) | Ours(Opus) | Permute | Sentinel |
|---|---|---|---|---|
| 德国口径 | `Patents_info` 里 `DE-\d` 前缀 | 同 | 同 | 同（11,598 DE 专利）|
| 授权窗 | grant_date 解析 → 2019-07-01..12-31，cohort=34 | 同，34 | 同 | 同，34 |
| CPC 取值 | `first=true` 主分类 | 同 | 同 | 同 |
| level 4 | code 前 3 字符，patent × group 去重 | 同 | 同 | 同（39 pair / 23 group）|
| EMA | α=0.1，年区间补零 | 同（并对 adjust=True/False 做了消融）| 同 | 同（`ewm(adjust=False)`）|
| 组数（算出）| **23**（10/10 attempt 轨迹里都出现 A21）| **23** | 23 | 23 |
| 最高级作用域 | **筛组**：只留 EMA 峰值最高的 2 组 | 同（4/5）；1 次判为「每组各一行」| **每组内选年**：23 组全给 | **每组内选年**：23 组全给 |
| 交付行数 | 2 | 2（4 次）/ 23（1 次）| 23 | 23 |

计算面**四方完全一致**（cohort 34、group 23、A61@2016 与 H04@2015 峰值 2.0、其余峰值 1.0，四方数值全部对得上）。

### 1.4 分歧点定位

唯一岔口在**「最高级修饰谁」**，而且岔口发生在**算完之后的写出**这一步。

我方 flash attempt_001 把这个判断写成了明文（第 60 条 assistant）：

> Independently confirmed. Only H04 (2 patents filed 2015) and A61 (2 patents filed 2016) reach a peak
> EMA of 2.0; all other level-4 groups peak at 1.0. **These are the two CPC technology areas with the
> highest EMA.** Let me write the answer.

紧接的交付是一段**手打字面量**：

```python
answer = """ELECTRIC COMMUNICATION TECHNIQUE - H04 - 2015
MEDICAL OR VETERINARY SCIENCE; HYGIENE - A61 - 2016
"""
with open("/workspace/answer.txt", "w") as f:
    f.write(answer)
```

Opus attempt_002 更刺眼：它先算出 23 行的 DataFrame

```
[AUTO-INSPECT] DataFrame shape=(23, 4) dtypes=(y_adjF:float64, ema_F:float64, y_adjT:float64, ema_T:float64)
     y_adjF  ema_F  y_adjT  ema_T
H04  2015.0    2.0  2015.0    2.0
A61  2016.0    2.0  2016.0    2.0
F02  2010.0    1.0  2010.0    1.0
```

然后同样手打两行字面量交付：

```python
ans="""ELECTRIC COMMUNICATION TECHNIQUE, H04, 2015
MEDICAL OR VETERINARY SCIENCE; HYGIENE, A61, 2016
"""
open('/workspace/answer.txt','w').write(ans)
```

而唯一通过的 Opus attempt_004，交付语句是**从结果对象序列化**的：

```python
lines=[f"{r.title} | {r.code} | {r.best_year} | EMA={r.ema:.4f}" for r in fin.itertuples()]
txt="\n".join(lines)
open("/workspace/answer.txt","w").write(txt+"\n")
```

程序化核对（10/10 attempt）：`A21` 在**每一条**轨迹里都出现过；`shape=(23,` 在 3 条里直接出现；
9/10 attempt 最终只写了 2 行。**算全 → 手打 → 截断**是稳定的失败链路，与模型档位无关（flash 5/5 犯，Opus 4/5 犯）。

### 1.5 交付门的表现（负面证据）

Opus attempt_001 / attempt_003 触发了现有 delivery gate 并被改写，`gate.json`：

```json
{"violations": ["the answer glues fields together with ' - ' separators like a database dump; ..."]}
```

改写后 `answer.txt` 形态变干净了，**行数仍是 2**，仍然 fail。两条校准结论：

1. 现有 `_COUNT_RE` 只认 `top N` / `N most` / `list the N` 这类**题面里带数字**的基数约束；
   `for each X` 类问题在题面上拿不到 N，现有门对这一整类零覆盖；
2. 现有 `_TABLE_ROW_RE`（markdown 表格判违规）被证据否定 —— Permute 的 5/5 通过答案全部是纯 markdown 表格。
   这条检查在消耗改写调用，且方向可疑。

### 1.6 根因分类

**交付形态（主）+ 解读歧义（次）**。计算面零缺陷，方法自由度不涉及，稳定性只体现为 Opus 的 1/5 抽中。

### 1.7 通用修复候选

| # | 机制 | 落点 | 自检：是否含题目特定内容 |
|---|---|---|---|
| P1 | **结果集序列化交付**：新增 `submit_answer(rows=...)` 工具，答案由最终结果对象（DataFrame / list）序列化产生；手写 `open(answer.txt).write(字面量)` 路径在评测模式下降级或告警 | 工具层 `raven/plugin/data_agent/tools.py` | 否。「交付必须来自算出来的对象、不许重打一遍」对任何仓库任何任务都成立 |
| P2 | **交付行数对账**：门在 transcript 里取 agent 最后一个表格型结果的行数（或要求 agent 通过 P1 显式声明 N），与交付条目数比对，不等即违规 | delivery gate（新检查类：cardinality reconciliation） | 否。规则只比较「你算出几行」和「你写出几行」，不涉及任何数据集语义 |
| P3 | **最高级作用域判定**：题面出现 `highest/best/most` 时必须显式判定它修饰「选哪些组」还是「每组内选哪个点」；当同一句里存在 `for each X` / `per X`，作用域归后者，交付面是每组一行 | methodology §1（读题即契约）+ §2（先定形状与粒度）| 否。这是自然语言里最高级与全称量词的作用域规则，与 CPC 无关 |
| P4 | **收回 markdown 表格违规**（或降级为不触发改写的提示）；同时把 `_COUNT_RE` 的适用面在文档里标注为「题面显式基数」，其余基数交给 P2 | delivery gate 校准 | 否。这是对门自身检查项的证据校准 |

补充：methodology §4 在 L21 已经写入「`For each X` means every X in the population」与「交付前对分组列 distinct
值逐一对账」。flash L21 的 5/5 attempt 全数违反。**这是「散文触发弱」的第 5 次验证** —— P2/P1 必须是代码门，不是新增段落。

---

## 2. crmarenapro query12

### 2.1 题面与政策

```
Who had the quickest average turnaround from opening to closing opportunities among agents in
April 2023? Return only the Id of the agent.

## Sales Cycle Policy
- Definition: The sales cycle is measured as the number of days between an opportunity's creation
  date and the company signed date on the corresponding contract.

## Today's date: 2024-09-12
```

政策做了两件事：定义区间为 `Opportunity.CreatedDate → Contract.CompanySignedDate`；
并且**重命名了「closing」这个事件** —— 在本题语境下，"closing" 就是 company signed。

### 2.2 四方答案与得分

| 方 | 得分 | 答案 | 稳定性 |
|---|---|---|---|
| Ours(flash) | 0/5 | `005Wt000003NJgAIAW` ×5 | 完全确定性 |
| Ours(Opus) | 0/5 | `005Wt000003NJgAIAW` ×5 | 完全确定性 |
| Permute | 5/5 | `005Wt000003NDEBIA4` ×5 | 确定性 |
| Sentinel | 5/5 | `005Wt000003NDEBIA4` ×5 | 确定性 |

期望值 `005Wt000003NDEBIA4`。两个模型跨 10 次运行给同一个错答案 —— 这是**系统性读法差**，不是抽签。

### 2.3 各方解读链对比

| 决策点 | Ours（两模型一致）| Permute | Sentinel |
|---|---|---|---|
| (a) 区间两端 | `Opportunity.CreatedDate` → `Contract.CompanySignedDate` | 同 | 同 |
| join | `Opportunity.ContractID__c` = `Contract.Id`，两侧剥 `#` 与空白 | 同（163 行全命中）| 同（163 行全命中）|
| (b) `in April 2023` 挂在哪 | **`Opportunity.CreatedDate` 落在 2023-04** | **`Contract.CompanySignedDate` 落在 2023-04** | 同 Permute |
| (c) 人群 | 有合同的 opportunity（163）| 同 | 同 |
| (d) 每 agent 平均 | 该 agent 在该 cohort 内 cycle_days 的均值 | 同 | 同 |
| cohort 规模 | 3 行 / 3 agent | 1 行 / 1 agent | 1 行 / 1 agent |
| 结果 | min = NJgAIAW（49 天）| NDEBIA4（304 天）| NDEBIA4（304 天）|

**(a)(c)(d) 四方完全一致，唯一岔口是 (b)。**

### 2.4 分歧点定位：我们把三种 cohort 都算了，然后选错

Opus attempt_001 第 10 条工具调用，同时算了 `csd`（company signed）与 `created` 两个 cohort：

```
csd 1
                ownerid                 oid    created        csd  days
155  005Wt000003NDEBIA4  006Wt000007BI41IAG 2022-06-15 2023-04-15   304
ownerid
005Wt000003NDEBIA4    304.0

created 3
85   005Wt000003NJgAIAW  006Wt000007BChmIAG 2023-04-25 2023-06-13    49
92   005Wt000003NISMIA4  006Wt000007BDApIAO 2023-04-10 2023-10-13   186
150  005Wt000003NEa3IAG  006Wt000007BHPhIAO 2023-04-15 2023-09-30   168
ownerid
005Wt000003NJgAIAW     49.0
```

第 12 条又补了第三种（`Opportunity.CloseDate` 落在 4 月）：

```
                     mean  count
ownerid
005Wt000003NJjNIAW   81.0      1
005Wt000003NJgAIAW  105.0      1
005Wt000003NFB8IAO  196.0      1
```

**正确答案 `005Wt000003NDEBIA4` 在第 10 条就打印出来了。** 随后的 assistant 消息 `content` 为空
（无 thinking 记录），直接写：

```python
open('/workspace/answer.txt','w').write('005Wt000003NJgAIAW\n')
```

`NJgAIAW` 是 `created`-in-April cohort 的最小值（49 天）。**裁决没有留下任何理由。**

程序化核对（10/10 attempt）：`005Wt000003NDEBIA4` 出现在**全部 10 条轨迹**里；8/10 直接算出了 304 天那一行。
10/10 仍然交付 `NJgAIAW`。

对照 Permute：同样枚举了 5 种 cohort（`company_signed` / `customer_signed` / `contract_start` / `opp_created` / `opp_close`），
拿到完全相同的三张表，然后在 `select_result_candidate` 工具里**被迫写下裁决理由**：

> Cohort is April 2023 applied to the closing endpoint Contract.CompanySignedDate per the binding
> temporal constraint; ... The competing signed-date alternative (CustomerSignedDate in April 2023)
> returns the same agent; **the StartDate and Opportunity.CloseDate cohorts are rejected because the
> policy and temporal constraint define closing as the company signed date.**

（值得注意：Permute 第一次提交裁决被工具**驳回**，理由是 `Selection must preserve the candidate's evidence
provenance: probe_recipe:mixed_temporal:Contract.CompanySignedDate, time_field:...` —— 裁决点是硬校验的，
补齐 evidenceRefs 后才放行。）

Sentinel 的说法更短但同构：

> Per the Sales Cycle Policy, "closing" is the contract's CompanySignedDate.

### 2.5 根因分类

**政策优先级（主）+ 解读歧义（次）**。政策段重定义了 "closing" 这个事件，而题目的时间限定 `in April 2023`
修饰的是 "closing opportunities"。我们把限定挂到了区间**起点**（creation），等价于忽略了政策对事件命名的约束。
稳定性不是问题（10/10 一致），交付形态不是问题（单 Id）。

更本质的一层：**我们有枚举，没有裁决。** 候选读法全算了，选择却是无记录的隐式动作。

### 2.6 通用修复候选

| # | 机制 | 落点 | 自检：是否含题目特定内容 |
|---|---|---|---|
| Q1 | **结构化裁决点**：新增 `commit_reading` / `select_result` 工具，写答案前必须提交〔选定读法 / 被否读法清单 / 理由 / 依据列名〕；交付门在缺少裁决记录且轨迹中存在 ≥2 个同型候选结果时告警 | 工具层 + delivery gate | 否。「多候选读法必须显式裁决并留证」是通用分析纪律，不含任何字段名或数值 |
| Q2 | **时间限定的挂载点规则**：区间型指标（turnaround / cycle / lead time / duration）叠加时间限定时，限定默认落在**终点事件**（closing/completion/delivery 那一端），除非题面显式说 opened-in / created-in；两端读法都算一遍并在裁决点记录 | methodology §1 | 否。这是「事件区间 + 时间窗」这类问法的通用语义，任何工单/订单/交易域都适用 |
| Q3 | **政策段的术语绑定优先级**：附带 policy/definition 段时，段里被重新定义的每一个术语，必须在题干中逐个替换后再解题；术语替换表要显式列出（如 closing := <policy 指定的字段>）后才开始查询 | methodology §1 | 否。规则只说「政策重定义术语要回代题干」，不预设任何具体术语 |
| Q4 | **候选枚举必须收敛为唯一裁决**：轨迹中出现 ≥2 组同型候选结果而最终答案未标注选中项时，视为未完成 | delivery gate（新检查类：unresolved-candidate）| 否。检查的是流程完备性 |

---

## 3. crmarenapro query2

### 3.1 题面与政策

```
Does the cost and setup of this quote comply with our company policy? If it doesn't, which knowledge
article is it in conflict with? Return only the Id of the knowledge article that the quote violates.
If no violation is found, return None.

## Quote approval guide.
Look for relevant knowledge articles to justify the quote approval.

- Quote Id to be considered is: 0Q0Wt000001WSDVKA4
```

这是一道「从候选政策集合中指认唯一命中项」的题 —— 候选空间由数据自己给出（`knowledge__kav` 表），
不由题面给出。

### 3.2 四方答案与得分

| 方 | 得分 | 答案 | 稳定性 |
|---|---|---|---|
| Ours(flash L21) | 0/5 | `ka0Wt000000Ens5IAC` ×5 | 稳定（更早的 flash 轮次曾 3/5，属跨轮漂移）|
| Ours(Opus) | 3/5 | `ka0Wt000000Eq0MIAS` ×3、`ka0Wt000000Ens5IAC` ×2 | **不稳定** |
| Permute | 5/5 | `ka0Wt000000Eq0MIAS` ×5 | 稳定 |
| Sentinel | 0/5 | `ka0Wt000000Ens5IAC` ×5 | **稳定地错** |

期望值 `ka0Wt000000Eq0MIAS`（Volume-Based Discounts）。`ka0Wt000000Ens5IAC` = Mandatory Bundles for Quotes。
Sentinel 在这题上是**反面对照组**：它展示了一个稳定的错读法长什么样。

### 3.3 两种读法的分水岭

目标 quote 的 4 条 line item（四方读到的数据完全一致）：

| 产品 | 数量 | 折扣 | 行金额 |
|---|---|---|---|
| DesignWave Automation | 5 | 5% | 2279.95 |
| **EcoPCB Creator** | **8** | **15%** | 2379.93 |
| PulseSim Pro | 10 | 10% | 4499.91 |
| CircuitSync Pro | 7 | 5% | 2260.93 |

`Volume-Based Discounts` 文章的字面表述是**金额阈值**：

> 1. **5% Discount for Purchases Over $5** ... 2. **10% Discount for Purchases Over $10** ...
> 3. **15% Discount for Purchases Over $20**

- **金额读法**：每行都远超 $20，全部有资格拿 15% ⇒ 折扣面不可能违规 ⇒ 这篇文章不成立，转向下一个候选（Mandatory Bundles）。
  Sentinel 与我方 flash 全部走这条路。
- **数量读法**：阈值键在 `Quantity`（5 / 10 / 20 件）⇒ EcoPCB Creator 数量 8 只该拿 5%，实拿 15%，超标 ⇒ 违规命中。

题面本身无法裁定，**只能靠数据**。

### 3.4 分歧点定位：判别力检验做了就对，不做就错

**Permute 的裁决记录**（`select_result_candidate.reason`，一段话同时否掉两个错读法）：

> Quote 0Q0Wt000001WSDVKA4 has 4 line items; one (EcoPCB Creator, qty 8) carries a 15% discount while
> the Volume-Based Discounts article allows only 5% at volume 5-9 (10% at 10+, 15% at 20+).
> **The quantity-tier reading is validated dataset-wide (2659/2966 lines exactly on tier, 0 below tier)**,
> so the discount excess is a genuine cost-policy violation. **The competing Mandatory Bundles
> interpretation was rejected because it is violated by 209/209 quotes containing PulseSim Pro (and
> nearly all bundle-parent quotes), i.e. non-discriminating background rather than a quote-specific
> violation**, and the installation-timeline policy has no quote-level setup data to evaluate.

两条判据都是**总体统计**，不是单条记录的检查：

1. 数量读法的支撑 —— 全量 line item 里「恰好等于档位」占绝对多数，且**越界是单侧的**（0 条低于档位）。
   单侧越界正是「这是一个被执行的上限」的签名；
2. Mandatory Bundles 的否决 —— 在**全体** quote 上命中率接近 100%，没有判别力，是背景噪声不是本 quote 的违规。

**我方 Opus attempt_001（通过的那次）做了完全相同的两件事。** bundle 基线：

```python
pulse = set(qq[qq.Name=='PulseSim Pro'].qid)
...
209 Counter({(False, False): 194, (True, False): 9, (False, True): 6})
```

即 209 个含 PulseSim Pro 的 quote 中，**0 个**同时含齐两个捆绑件。档位检验：

```python
def tier(q):
    if q>=20: return 15
    if q>=10: return 10
    if q>=5: return 5
    return 0
qq['over']=qq.Discount>qq['exp']; qq['under']=qq.Discount<qq['exp']
print(qq.over.mean(), qq.under.mean())
```

```
0.10350640593391773 0.0      # 10.35% 的行超档，0% 的行低于档 —— 单侧越界
```

它还进一步把两个候选和 quote 状态做了交叉表，确认 bundle 违规在 `Accepted`（149/163）、`Approved`（184/204）
里同样普遍 —— **与审批结果无关**，坐实无判别力。随后交付 `ka0Wt000000Eq0MIAS`，通过。

**我方 Opus attempt_003（失败的那次）两件事一件没做。** 它读了折扣文章的开头（只打印到第 1 档），
从未把 `Discount` 和 `Quantity` 放在一起比，从未做任何总体统计，只在目标 quote 内部找到缺失的捆绑件，直接收工：

```python
print(repr(ka[ka['title'].str.strip()=='Mandatory Bundles for Quotes']['id'].iloc[0]))
open('/workspace/answer.txt','w').write('ka0Wt000000Ens5IAC\n')
```

**Sentinel 是同一病灶的极端形态。** 全轨迹只有 9 次工具调用，逐条列出：查目标 quote → 列 kav 标题 →
连 PG → 读 6 篇候选政策全文 → 查目标 quote 的 line item → 查产品名。**没有任何一次查询触及目标 quote 以外的总体。**
于是它在 5 次运行里给出同一段自信的结论：

> **Volume-Based Discounts** (max 15% for purchases > $20): all discounts are 5-15% on subtotals > $20
> → compliant. ... **Mandatory Bundles for Quotes**: VIOLATION — the quote includes PulseSim Pro,
> which requires CircuitMaster Analyzer and VeriSim Express ... Neither is present.

单记录核查 + 政策字面读法 = 稳定的错答案。

### 3.5 根因分类

**方法自由度（主）+ 解读歧义（次）+ 稳定性（表征）**。判别力检验这项能力我方**已经具备**（attempt_001 完整演示），
但是否触发全凭当轮采样 —— 于是 Opus 得到 3/5 的散射，flash 5/5 不触发。
解读歧义那一层（阈值单位是金额还是数量）本身不可从题面裁定，只能由总体统计裁定，所以两层其实指向同一个机制。

### 3.6 通用修复候选

| # | 机制 | 落点 | 自检：是否含题目特定内容 |
|---|---|---|---|
| R1 | **判别力检验（discriminativeness / base-rate check）**：当任务是「从候选集合中指认唯一命中项」（哪条规则 / 哪个原因 / 哪个异常），对每个候选计算**总体命中率**；在总体上近乎全命中或全不命中的候选不具判别力，须排除；答案应落在「总体稀有、目标命中」的候选上 | methodology §6（提交前验证）新增小节 | 否。归因 / 合规 / 根因 / 异常检测全类适用，不含任何字段、阈值或文章名 |
| R2 | **阈值量纲歧义的数据裁定**：政策文本给出的阈值与数据里的候选列量纲不匹配时（金额 vs 数量 vs 比例），把每种读法都在**全量**上算一遍，用一致性签名选：若某读法下总体越界是**单侧**的（只有超标没有欠标），该读法即是被执行的口径 | methodology §1 或 §3 | 否。这是「文本阈值 ↔ 数据列」对齐的通用判据 |
| R3 | **单记录结论必须有总体背书**：当答案是「某条记录违反了某条规则」时，交付前必须至少有一次**跨全体记录**的统计查询；只查目标记录就下结论，视为验证不完整 | methodology §6 + delivery gate（可检测：轨迹中是否存在不带目标主键过滤的聚合查询）| 否。约束的是取证广度，不是任何具体数据 |
| R4 | 复用 **Q1 结构化裁决点** —— 本题的裁决理由结构（选中候选 + 被否候选 + 否决依据）与 q12 完全同型 | 工具层 | 否 |

---

## 4. 跨题综合

### 4.1 三题的失败位置在同一层

| 题 | 算对了吗 | 失败发生在 | 类别 |
|---|---|---|---|
| PATENTS q2 | **是**（10/10 轨迹都算出全部 23 组）| 写出去时手打字面量，只写 2 行 | 交付 |
| crmarenapro q12 | **是**（10/10 轨迹都打印出 NDEBIA4，8/10 算出 304 天）| 三个 cohort 里无记录地选错一个 | 裁决 |
| crmarenapro q2 | **有时**（Opus 3/5 做了判别力检验）| 判别方法非强制触发 | 裁决（+稳定性）|

**没有一题输在「不会算」。** 三题全部输在算完之后的两个动作：**裁决**（选哪个结果）与**交付**（怎么写出去）。
这与 R16「机制层已闭合，剩余差距 = 模型档位」的结论直接冲突 —— 至少这三题不是档位问题，
因为 Opus 5 和 flash 在 q12 上给出**完全相同**的错答案，在 PATENTS 上犯**完全相同**的截断。

### 4.2 单一收益最大的两个通用机制

**机制一：结构化裁决点（Q1/Q4/R4）。** Permute 的 `select_result_candidate` 是它三题全过的共同前置：
它强制「选中 + 被否 + 理由 + 依据列」四件套，还会因 evidence provenance 不全而**驳回**提交。
我们的轨迹在同一位置是一片空白（assistant `content` 为空，直接 write 文件）。
这一个机制同时覆盖 q12（读法裁决）与 q2（候选政策裁决），是本次归因里性价比最高的一项。

**机制二：结果集序列化交付 + 行数对账（P1/P2）。** 覆盖 PATENTS q2，并且顺带消灭「手打答案时漏项 / 改写实体」
的整类风险。它不需要模型更聪明，只需要交付路径不经过人手重打。

判别力检验（R1）排第三：它只在「指认唯一命中项」这一类题上生效，但那一类题在 CRM / 合规 / 根因域占比不低，
而且我们的能力已存在、只差强制。

### 4.3 量化：这三题占对 Permute 差距的多少

- PATENTS 共 4 题：q2 从 1/5 → 5/5，数据集 pass@1 **+0.20**；
- crmarenapro 共 14 题：q12 从 0/5 → 5/5（**+0.0714**），q2 从 3/5 → 5/5（**+0.0286**），合计 **+0.100**；
- 11 数据集分层平均：(0.20 + 0.100) / 11 = **+0.027**。

对 Permute 的差距是 0.8724 − 0.8382 = **0.0342**。**这三题单独就占 ~80%。**
换言之：把「裁决 + 交付」这一层补上，我们与 Permute 的距离主要就不再是分数问题，而是同带内的抽签问题。

### 4.4 对现有交付门的两条校准结论

1. `_COUNT_RE` 只覆盖题面显式基数（`top N` / `N most` / `list the N`）。`for each X` 类问题的基数来自**数据**，
   门必须从 agent 自己的结果对象取 N（P1/P2），而不是从题面正则；
2. `_TABLE_ROW_RE`（markdown 表格判违规）被 Permute 的 5/5 通过答案直接否证 —— 该检查在触发无谓改写。
   建议降级为不触发改写，或删除。

### 4.5 一条纪律层的复核

methodology §4 在 L21 已经写入「`For each X` means every X in the population」以及「交付前对分组列 distinct 值
逐一对账」。PATENTS q2 上，flash L21 的 5 次 attempt **全部违反**这两条。
这是「散文触发弱」的**第 5 次**独立验证（前四次记录在迭代日志 R15b）。
本文所有修复候选据此定档：**只要能做成代码门或工具签名，就不要再写成方法论段落。**

---

## 5. 修复候选汇总（按落点）

| 落点 | 候选 | 覆盖题 | 优先级 |
|---|---|---|---|
| 工具层 | Q1/Q4/R4 结构化裁决点（`commit_reading` / `select_result`，含被否候选与依据列必填）| q12、crmarena q2 | P0 |
| 工具层 | P1 `submit_answer(rows=...)` 结果集序列化交付 | PATENTS q2 | P0 |
| delivery gate | P2 交付行数 ↔ 计算行数对账（cardinality reconciliation）| PATENTS q2 | P0 |
| delivery gate | Q4 未裁决候选检测（≥2 组同型候选结果而无裁决记录）| q12 | P1 |
| delivery gate | R3 单记录结论需总体背书（轨迹中须存在非目标主键过滤的聚合查询）| crmarena q2 | P1 |
| delivery gate | P4 收回 markdown 表格违规、标注 `_COUNT_RE` 适用面 | 全局校准 | P1 |
| methodology §6 | R1 判别力检验（候选命中率 / base rate）| crmarena q2 | P1 |
| methodology §1 | Q2 时间限定挂载在区间终点事件 | q12 | P2 |
| methodology §1 | Q3 政策段术语回代题干（术语替换表）| q12 | P2 |
| methodology §1/§3 | R2 阈值量纲歧义的单侧越界裁定 | crmarena q2 | P2 |
| methodology §1/§2 | P3 最高级作用域判定（`for each` 优先）| PATENTS q2 | P2 |

**全表自检**：以上 11 条无一提及 CPC / Opportunity / Contract / knowledge article / 任何具体列名、阈值、
实体或期望值；每条的判据都是「问题怎么问、数据怎么长、结果怎么写」，可原样搬到任意仓库与任意任务。
methodology 类（P3/Q2/Q3/R1/R2）按 4.5 的纪律**降级为辅助**，主力放在工具层与 gate。
