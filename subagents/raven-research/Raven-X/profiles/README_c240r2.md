# `*_c240r2.json` —— dr@3.0 语料轴首测批(BC-Plus 240 题)的批次本地配置

写于 20260812,**Test session** 写。放 sidecar 不放 JSON 里:`raven/config/loader.py` 的
schema 是 `extra_forbidden`,往配置里加一个说明键 = raven 启动即死。20260811 已因此烧掉
一整条臂(24 题),而当时**九道前置全绿** —— 它们全都只做 `json.load`。⇒ 说明一律进本文件。

## 两份文件

| 文件 | 来源 | 与主线的差异 |
|---|---|---|
| `student_sglang_bcp_base_c240r2.json` | `student_sglang_bcp_base.json` | **逐字节相同**(`diff` 已验) |
| `student_sglang_bcp_dr_c240r2.json` | `student_sglang_bcp_dr.json` | **只多一处**:`drFlow.search.saturation`(排序键 diff:+7 行 / −0 行) |

```json
"saturation": { "enabled": true, "identity": "url", "k": 10, "onSaturate": "widen", "maxPages": 2 }
```

五个键名与 `DRFlowSearchSaturationConfig` 的 pydantic 别名逐字一致。起批前已用 raven
**自己的加载器**(`load_raven_config`,不是 `load_config` —— 后者不解析 drFlow)复验:

```
student_sglang_bcp_base_c240r2   drFlow.enabled=False  saturation.enabled=False   ← 锚点结构上到不了
student_sglang_bcp_dr_c240r2     drFlow.enabled=True   k=10 on_saturate='widen' max_pages=2
```

## ★ 语料轴上这把梯子只有**两级**

`web.py:274-275` 在 `corpus_endpoint` 存在时**强制** `saturation.paginates = False` ——
定长语料服务收 width 不收 offset。实测(`SearchSaturation` 直接驱动,`paginates=False`):

| 连续干搜次数 | `sat_action` | 效果 |
|---|---|---|
| 第 10 次 | `widened` | 渲染宽度 5→10,**同一次调用,免费** |
| 第 20 次 | `stopped_degraded` | `_degraded=True`,与普通 `stopped` **分开记** |

⚠️ 同一个 `k=10`,**web 轴是三级(10 widen / 20 paginate / 30 stop)** ⇒
**语料轴早 10 次干搜收手,更激进**。两轴的"同一个旋钮"不是同一个强度,
报数时不许把两轴的省检索率并排当同一个量。

`_degraded` 这个标志本身是好设计:一个配成 paginate 却跑在不能翻页的端点上的臂,
行为与配成 stop 的臂**一模一样**,而配置说的是另一回事 —— 能力开着、激活点不可达、
且没有症状。`sat_action` 报**它做了什么**而不是**它被配成什么**,把这个失败形态变成可读的。

## 与 `*_b302r2.json`(同日 web 轴批)的关系

同一个 `saturation` 五元组,**逐字相同**。两批一起构成 dr@3.0 的两轴首测。
⚠️ 但**两轴的 dr−base 不可相加、不可互换**(CLAUDE.md 口径:三条轴不可相加),
且如上,同一个 k 在两轴上的强度不同。

## 为什么不改主线

`pipeline/configs/` 是 Framework 的面。批次本地副本让 Framework 可以随时改主线而不影响
在跑的批次(何况 `lib/isolate.sh` 已把 `configs/` 一起快照)。
主线 `student_sglang_bcp_{base,dr}.json` 未被本批改动过一个字节;
两条消融臂配置(`_nodedup` / `_noev`)本批不用,也未改动。
