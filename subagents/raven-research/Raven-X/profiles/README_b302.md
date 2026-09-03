# `student_sglang_web_{base,dr}_b302.json` —— 本批(web_dr29_20260811,302 池)专属副本

**20260811 Test 建。** 与 `student_sglang_web_{base,dr}.json` 的**唯一差别** =
`tools.web.benchmarkContainment` 由 `true` 改回 **`false`**。

## 为什么
本批唯一用途是与 `eval_web_dr27_20260807` 的 302 行同题相减,而那批是 `false`
(该批 `.containment_ledger` = false)。containment 会拦掉 benchmark 自家域名 ⇒
**改变检索分布** ⇒ 开着跑就不可比。围栏策略是**批次级**的(它有自己的 ledger),
不是 dr@2.9 的版本属性。

## 为什么不改主线
主线 `student_sglang_web_*.json` 在 **08-10 20:06**(dr28 收批之后)被翻成 `true`,
很可能是为下一批准备的。Test 不该替 Framework 决定树上的围栏策略。
泄露风险改由跑后 `leakscan` 披露。

## ⚠️ 为什么这段话在这里而不在 JSON 里
第一版我把它写成 JSON 里的 `_note_test_20260811` / `_derived_from` 两个键,
**raven 启动即崩**:配置 schema 是 `extra_forbidden`(`raven/config/loader.py:195`),
多一个键就 `ValueError`。症状是每题 13 秒、0 轮、空答 —— 而九道前置**全绿**,
因为它们都只 `json.load` 读字段,**没有一道走 raven 自己的加载器**。
⇒ 已在 runner 加前置④d:用被测程序的加载器实跑一次。

---

## 20260812 (Framework): 两份配置已移进 `retired/`

**移动原因不是退役，是分类。** 它们是**已完成批次 `eval_web_dr29_20260811` 的批次专属副本**,
不是模板。留在 `configs/` 顶层会让版本门每批都红 —— 而「让每批都红的判据等于没有判据」。
门自己文档里给的出路就是 `retired/`(「它们真的是旧版本,改标签等于说谎」)。

**为什么不改标签**:标签必须继续描述那批真正跑过的东西。已核 —— 移动前
`pipeline/configs/student_sglang_web_dr_b302.json` 与
`data_dr/runs/eval_web_dr29_20260811/_iso/pipeline/configs/student_sglang_web_dr_b302.json`
**逐字节相同**,`version=dr@2.9`。该批的权威溯源是 `_iso` 快照与 `arm_env.json`,不是这份副本。

**为什么不改引用它的两个 runner**(`run_web_dr29_20260811.sh` · `resume_dr29_base_arm.sh`):
它们是冻结件。移动后它们会因**缺文件而响亮失败**,这正是正确行为 —— 在 flow 语义已经
走到 `dr@3.0` 的 build 上续跑一个 `dr@2.9` 批次,本来就该被拦住。要真的续跑,
应当从 `_iso` 快照起,那里代码与配置是一套的。
