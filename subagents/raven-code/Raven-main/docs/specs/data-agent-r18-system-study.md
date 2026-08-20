# R18b · 运行时 system-study 设计稿

状态:待 Ethan 过目。实现前不动代码。

## 1. 要解决的问题(证据链)

三类残余失败共享同一个上游缺口——**开工前对数据系统的认识不足**:

| 失败 | 缺的认识 | 现有机制为何够不着 |
|---|---|---|
| PANCANCER q1(全场四方 0/5) | 临床表有两个 histology 列;题面"排除方括号值"只对 `icd_o_3_histology` 成立——修饰语是列指纹 | 列选择发生在第一条 SQL 之前,gate/裁决点都在下游;methodology 列指纹规则(L21)是散文,不点火 |
| crm q2(判断 flaky) | 政策阈值(5/10/20)与 Quantity 列的量纲对应关系,数据里"恰好压档"签名早已存在 | agent 不知道有第二种读法 → 不会主动裁决(R18a 实证) |
| EQ 0.9133 vs 我们 0.84 档 | EQ 的差异化 = 离线学出的"数据系统证据层"(哪列可信、什么语义、怎么 join) | 我们每个 attempt 都从零现场摸,浅尝辄止 |

**方向**:把 EQ 的证据层做成 **运行时、每 attempt 独立、纯数据驱动** 的形态——rubric 白名单行为("profiling the sanctioned data"),不预置任何 DAB 内容,与 EQ 的离线冻结注入(tuned=Yes 的原因)划清界限。

## 2. 机制:`survey_sources` 工具(插件第 11 个)

一次调用,零 LLM,纯 SQL 探针,对所有已挂载 source 做结构化深勘,产出两样东西:

1. **返回值**(有界,≤4KB):按 source→table 组织的紧凑清单;
2. **工作笔记** `/workspace/survey.md`:全量细节,agent 后续按需查。

### 每列采集(全部机械可判)

| 信号 | 探针 | 治哪类病 |
|---|---|---|
| 类型/distinct/null 率/top-5 值 | 现有 profile 逻辑批量化 | 列选择盲区 |
| **同义列组**:名称含同一词根的列(histology/histological、date 家族) | 列名 token 分组 + 两列 distinct 对比 | PANCANCER 双列陷阱 → 输出 "2 columns mention 'histology': names(3 distinct) vs codes(23 distinct, 4 bracketed values)" |
| **脏值签名**:方括号值、占位符(NA/unknown/#)、混合类型 | 值形态正则 | 列指纹规则从散文变成摆在面前的事实 |
| **量纲候选**:小整数域(qty 形态)vs 金额形态(小数/大数)vs 比例形态(0-1/百分号) | 数值分布分档 | crm q2 的"阈值挂哪列"歧义显性化 |
| 主键性/行数/FD 层级(父子列对) | grain_check 逻辑批量化 | 粒度/join 扇出 |
| 跨表 join 候选(同名列 + 键形态匹配度) | verify_join 的 count 探针轻量版 | join 盲区 |

### 触发方式(散文六连败的教训)

adapter 任务指令第一句强制:"Start by calling survey_sources once; its notes are your map of the data." —— 与 submit_answer 同款强制形态(实测 100% 采用 vs 散文 0%)。

### 成本护栏

- 每表列数上限 40、每列探针 ≤3 条 SQL、单 source 超 60s 截断(已采集部分照常输出);
- 大文本列(平均长度 >200 字符)只做长度/空值统计,不做值分布;
- 返回值截断到 4KB,细节全量落 survey.md;
- 预估:典型 DAB source 8-15s、~40 条 SQL,零 LLM 成本;agnews 型宽表 ~20s。**换取的是 agent 现场摸索的 5-10 个来回**(净时间可能为负,即省时)。

## 3. 通用性自检(tuned 红线)

- 工具输出 100% 来自当场 SQL 探针,无预置内容;
- 信号定义(同义列组/脏值/量纲/FD)是关系数据的通用性质,零 DAB 词汇;
- 每 attempt 独立重算,无跨题/跨轮携带,无冻结证据包 —— 与 rubric "agent's own exploration of the sanctioned data" 逐字对齐;
- 实现后过 prompt_contamination_check。

## 4. 验证计划(Opus 不发全量)

1. 单测:三个合成 fixture(双 histology 列/量纲歧义/join 扇出)断言信号出现;
2. 靶向 Opus k=3(~$15):PANCANCER q1 + crm q2(主靶)+ music q3 + stockindex q3(哨兵)——判据:PANCANCER q1 首次非零或轨迹显示列选择改变;哨兵不回退;
3. flash 全量(L23):对比 L22 0.7469,重点看 survey 开销是否拖累 agnews 家族(deadline 内核在,风险有限)。

## 5. 明确不做

- 离线预算证据包/跨 attempt 缓存(= EQ 的 tuned 形态);
- LLM 参与 survey(v1 纯机械;语义标注列用途留 v2 评估);
- 按题型路由 survey 深度(加变量,先测统一形态)。
