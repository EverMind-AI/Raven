# `profiles/` — 产生我们已发表数字的那些配置

这个目录的存在只有一个理由:**让这棵树上的东西,就是我们测出成绩的那个东西。**

在 2026-08-29 之前它住在 `pipeline/configs/`,而 `pipeline/` 不在任何 git 仓库里。
后果是:代码在 GitHub 上,而**决定代码怎么跑的那一半不在**——
克隆这个仓库的人拿到的是 flow 的实现,却看不到、也复现不了我们量出成绩的那个配置。

`pipeline/configs` 现在是指向这里的符号链接,所以 39 个冻结的历史 launcher 一个都不用动。

---

## 三类文件

| 类 | 文件 | 是什么 |
|---|---|---|
| **处理臂** | `*_dr*.json`、`student_sglang_web_dr*.json`、`student_sglang_bcp_dr*.json` | 开着 `drFlow` 的臂。这些是「dr」那一侧 |
| **锚点臂** | `*_base*.json`、`official_*.json` | `drFlow.enabled=false`。**它是仪器,不是对手** —— 见下 |
| **出货产品** | `student_sglang.json` | 产品 flow。与它同后端的 bench 臂只差 3 个旋钮,由 `check_bench_product_parity.py` 守着 |

`retired/` 是历史配置,**起批 validator 会拒绝它们**(版本标签已被 supersede)。
留着是因为它们记录了过去某一批实际跑了什么;不要拿它们起新批次。

---

## 读这些文件之前必须知道的四件事

**1. 有效值不等于写下来的值。**
`DRFlowConfig` 有几十个字段,一份配置只写它要偏离默认的那些。判断一条臂真正跑成什么样,
唯一可靠的办法是**经真实加载器解析**:

```python
from raven.config import load_raven_config
cfg = load_raven_config(Path("profiles/student_sglang_web_dr.json"))
print(cfg.dr_flow.spin_breaker.enabled)   # 有效值,不是 JSON 里有没有这个键
```

读 raw JSON 的键会给出错误答案:`populate_by_name=True` 意味着写 `cross_query_dedup`
(snake_case)一样能把旋钮打开,而只认 `crossQueryDedup` 的检查会是绿的。
这个错本仓库付过两次钱,`scripts/`(或 `pipeline/`)里的每一道配置门因此都走真实加载器。

**2. 「键缺席」与「显式写 false」有效值相同 —— 但默认一旦会动,它们就不再相同。**
2026-08-29 抬八个类默认之前,先把每一条**靠继承表达 off** 的配置改成了显式写 `false`。
理由是 `student_sglang_web_dr_nosnip`:它是「关掉 snippet」的消融臂,而它表达「关」
的方式就是不写那个键 —— 默认一抬,**它就不再是消融臂了,配置文件一个字没改。**

**3. 锚点臂(`drFlow.enabled=false`)拿不到任何 `drFlow` 下的东西。**
`build_dr_flow` 对它返回 `None`,于是整个 observer 链、三个 `finalShape` 从句、
研究附录全都结构上不可达。这不是配置漏了,这是锚点的定义。
所以在 bench 上给处理臂开 `finalShape.requireMarker` 之类的旋钮
= 只给一侧一个「答案更好抽」的优势,而那与研究质量无关。

**4. 两条轴的「dr − base」不是同一个对比。**
`DRFlowSearchConfig` 是 `drFlow` 的子配置 ⇒ flow-on vs flow-off **按定义**
包含 flow 设的每一个旋钮。语料轴只含 1 个 SERP 旋钮的差,live-web 轴含 3 个。
这是口径,不是缺陷。

---

## 端点

`WORKER3_IP` 是占位符,起批脚本会替换。少数配置里有内网地址与私有网关域名,
它们是内部评测端点,外部环境跑不通 —— 想在自己的环境复现,把 `providers` 段
换成你能到达的上游即可;`drFlow` 段才是这个目录真正要保存的东西。

---

## 相关的门

| 门 | 判什么 |
|---|---|
| `check_bench_product_parity.py` | 出货配置 ↔ **同后端**的 bench 臂,有效值一致性(白名单里每条分歧都写了理由) |
| `check_drflow_knobs.py` | 逐臂旋钮期望值,经真实加载器 |
| `check_version_sync.py` | `drFlow.version` 标签 ↔ 源码 |
| `check_config_loads.py` | 这份配置 raven 加载得动吗 |

⚠️ 「产品配置 vs bench 配置」**不是良定义的比较** —— 只有「vs **同后端**的 bench 配置」才是。
`thinkClosingTagRequired`、`search.saturation` 这类旋钮按**后端**分,跨后端比会把
后端差异算成产品/bench 差异。parity 门因此强制两份配置的 `model` 相同,不同就红。
