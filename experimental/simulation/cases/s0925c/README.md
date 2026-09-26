# s0925c：培养一个旅行社的 AI 顾问

一家虚构旅行社"海岚旅行"的老板培养他的在线 AI 顾问。老板不碰任何配置，只做三件事：上传店里的资料，扮成客人演练，演练后说出意见。Curator 据此改造顾问和它的三个子代理组成的 harness-of-harnesses。

这个案例要展示的是两种已经能看到的价值，而不是一个满足全部要求的完美结果：

1. **按资料定制**：入职时还没有任何接待记录，Curator 只凭资料就生成了流程和关口，之后三轮一直守住。
2. **按意见改进**：老板指出的问题，Curator 在下一轮前改到了执行层面，第 3 轮全部 11 条红线通过。

全过程由模拟自动运行，完整记录见 [transcript.md](transcript.md)。

## 设置

| 项 | 内容 |
|---|---|
| 被培养的员工 | 根 Harness：在线顾问；子 Harness：调研（Raven-Research）、写需求单（Raven）、做方案 PPT（Raven-PPT） |
| 模型 | Curator 与老板：Claude Opus 5.5；顾问、子代理与模拟客人：DeepSeek flash |
| 轮次 | 入职后 3 轮，每轮 1 张亲子家庭题卡；日期、出发地、孩子年龄等数值每轮重新抽取，Curator 学到的必须是规则而不是某个客人的答案 |
| 老板的资料 | 服务 SOP、价目表、预订规则、三档产品说明、接待话术、转交单模板、方案 PPT 规范、品牌设计规范、模板与样例，见 [scenarios/travel_agency](../../scenarios/travel_agency/) |

## 价值一：入职时凭资料定制

入职这次改造只动了根 Harness，四个策略面都用上了：

| 资料里的规矩 | Curator 生成了什么 | 三轮里的表现 |
|---|---|---|
| SOP：客人接受 → 写转交单 → 调研和做方案 → 质检 → 交付 | **planning**：重写做方案的 playbook（调研 → 需求单 → 制作三个节点），删去子代理直接交付，方案回到顾问手里质检；新增返工 playbook。**memory**：上岗手册写明每一步 | 顺序三轮都对；老板第 1 轮："客人接受后先交了占位申请，再去做调研和方案，这个顺序是对的" |
| SOP：每条消息最多问两个问题 | **action**：`hl_action.py` 在每条消息发出前数问号，超过就打回重写 | 三轮都通过；第 3 轮第 1 回合实际拦下一条问了 3 个问题的草稿 |
| SOP：店外信息只能来自调研同事 | **capability**：关掉浏览器、深度搜索和插件 | 三轮聊天里都没有出现未经查证的班次、门票或天气 |
| 方案规范：文件名"海岚旅行-报价编号.pptx"，套用店里模板 | **action**：交付前检查文件名；**planning**：制作节点固定传入模板路径 | 三轮都合规 |

生成代码节选（`hl_action.py`，入职时生成）：

```python
asked = text.count("？") + text.count("?")
if asked > 2:
    problems.append(f"一条消息里问了 {asked} 个问题，每条消息最多问两个问题")
```

## 价值二：按意见逐轮改进

| 老板说 | Curator 改了什么 | 下一轮 |
|---|---|---|
| 第 1 轮：客人只说了"我姓许"，就被称作"许女士"；付款节点第一次说错 | 顾问的手册加了称呼、付款节点两条规则；关口加查猜测的称呼、与出发天数矛盾的付款节点 | 第 2、3 轮改好 |
| 第 1 轮：方案里写着"来源：调研第 2 节"，班次写成"当天最快的一班" | 写需求单的子 Harness 加了写盘前的审核（action）；做 PPT 的子 Harness 加了自检工具（capability）和"导出后必须自检"的关口 | 第 2、3 轮改好 |
| 第 2 轮：门票被概括成"一人免票、一人半价"，调研里没有这个说法 | 需求单审核新增：门票说法按每个孩子的年龄与调研原文逐个核对 | 第 3 轮改好；审核在第 3 轮实际打回 4 次需求单 |
| 第 2 轮：确认需求时说"好，都齐了"，复述漏了有无 65 岁以上长辈 | 顾问的关口拦"齐了、收集完毕"，复述必须说明有无长辈 | 第 3 轮改好 |

第 2 轮后生成的门票检查节选（写需求单的子 Harness）：

```python
rules = _rules(research, free)           # 从调研原文取年龄规则
results = [(snippet, [pred(a) for a in ages]) for snippet, pred in rules]
if computable and counts_set == {claimed}:
    continue                             # 与逐个孩子核对的结果一致才放行
```

## 交付物

- [round1-HL-Q-1020-4P.pptx](round1-HL-Q-1020-4P.pptx)：第 1 轮的方案。封面猜了称呼"许女士"；第一天的班次写成"当天最快的一班"，来源写"调研第 2 节"。
- [round3-HL-Q-1028-4P.pptx](round3-HL-Q-1028-4P.pptx)：第 3 轮的方案。客人没给称呼，封面写"尊敬的客人"；班次标明是代表性排班，来源写真实网站，酒店标"参考酒店，以计调确认为准"。

两轮的日期、出发地和孩子年龄不同，是因为题卡数值每轮重新抽取。

## 过程记录

[transcript.md](transcript.md) 是系统从运行记录自动导出的培养记录，包括：入职对话与资料、每轮的演练对话与交付物、老板评审、交给 Curator 的要求、Curator 的理解与每个策略面的代码 diff、机制实际拦截的记录，以及价值判定。[provenance.json](provenance.json) 记录运行设置和原始记录文件的 sha256。

## 复现

按当时的设置重跑（需要自备带 key 的配置和员工基线 home；代码已继续演进，结果不会逐字相同）：

```bash
uv run python -m experimental.simulation --config <config.json> --home <员工基线 home> --state-dir <新的空目录> \
  --scenario travel_agency --chain documents --rounds 3 --turns 14 --repeats 1 --timeout 9000 \
  --analysis owner --cards family --seed 11 \
  --curator-model openrouter/anthropic/claude-opus-5.5 --simulation-model openrouter/anthropic/claude-opus-5.5 \
  --traveller-model deepseek/deepseek-flash --subagent-model deepseek/deepseek-flash \
  --curator-effort high --traveller-effort low
```

## 说明

- 这个案例用的是单层做法（`--analysis owner`）：老板的评审直接作为给 Curator 的要求。现在默认由通用 Analyst 从老板原话整理要求。
- 运行中做 PPT 的子代理曾用命令行搜索磁盘，没有读到评审侧的内容；之后的代码已关闭子代理的命令行。
