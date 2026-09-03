# `*_b302r2.json` —— dr@3.0 首测批(web 轴 · 302 池)的批次本地配置

写于 20260812,**Test session** 写(Framework 在转达里指定了内容,Test 只落盘 + 起批)。
放 sidecar 不放 JSON 里:`raven/config/loader.py` 的 schema 是 `extra_forbidden`,
往配置里加一个说明键 = raven 启动即死。20260811 已因此烧掉一整条臂(24 题),
且当时**九道前置全绿** —— 它们全都只做 `json.load`。⇒ 说明一律进本文件。

## 两份文件

| 文件 | 来源 | 与主线的差异 |
|---|---|---|
| `student_sglang_web_base_b302r2.json` | `student_sglang_web_base.json` | **逐字节相同**(`diff` 已验) |
| `student_sglang_web_dr_b302r2.json` | `student_sglang_web_dr.json` | **只多一处**:`drFlow.search.saturation` |

```json
"saturation": { "enabled": true, "identity": "url", "k": 10, "onSaturate": "widen", "maxPages": 2 }
```

五个键名与 `DRFlowSearchSaturationConfig` 的 pydantic 别名逐字一致,`"widen"` 在
`Literal["widen","paginate","stop"]` 内。起批前已用 raven **自己的加载器**
(`load_raven_config`,不是 `load_config` —— 后者不解析 drFlow)复验加载成功。

## ⚠️ 与 `*_b302.json`(dr@2.9 那批)差一个围栏,名字只差两个字符

| | `_b302`(dr@2.9,20260811) | `_b302r2`(dr@3.0,20260812) |
|---|---|---|
| `tools.web.benchmarkContainment` | **false** | **true** |

dr@2.9 逐臂 `arm_env.json` 实测 `{'value': False}` 两臂都是;主线两份配置于
08-10 20:06 被翻成 `true`,本批按 Framework「原样 + 只改一处」沿用 `true`。
围栏对两臂同时生效 ⇒ **批内可读**,且「开启需全批一致 + bump + 新锚点对」三条
本批都满足(bump 到 dr@3.0、四臂含新锚点对)。
⇒ **但它是本批锚点与 dr@2.9 锚点的第二处差异**(第一处是 A1 截断兜底由不 gate 改为
默认关),Framework 的转达只列了第一处。**跨批相减本就被禁,这里只作预注册披露。**

起批器前置 ④b/④d 都以 `--expect on` 验本批这两份 ⇒ 拿错文件会被门拦下,
不会静默跑成另一个围栏策略。

## 为什么不改主线

`pipeline/configs/` 是 Framework 的面。批次本地副本让 Framework 可以随时改主线而不影响
在跑的批次(何况 `lib/isolate.sh` 已把 `configs/` 一起快照)。
主线 `student_sglang_web_{base,dr}.json` 未被本批改动过一个字节。
