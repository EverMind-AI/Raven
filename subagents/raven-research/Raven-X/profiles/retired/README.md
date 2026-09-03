# 退役臂配置

这些配置的 `drFlow.version` 是已退役标签,当前 build 的验证器会**硬失败**(AGENTS.md §0.2:
旧标签跑在新 build 上 = 静默给整批数据贴错标签,拒绝启动比跑完再发现便宜得多)。

留档不删的理由:它们是当时那批数据的**分布凭证** —— 报告里引用 dr@1.2 那几批的分,
读者要能查到当时到底开了哪些旋钮。要复现只能连同当时的 build 一起复现,不能只改版本号。

- `student_sglang_dr12*.json` — dr@1.4,2026-06 的 A′ 批次(记忆 `a2-batch-dr12-5arm-verdict`)
- `dr32_{bcp_dr128,web_dr128,web_dr64}.json` — dr@3.2,两轴收官批
  (裁决 `data_dr/runs/VERDICT_dr32_20260817.md`)。20260817 dr@3.3 bump 时移入(Framework)。
  ⚠️ 三条**锚点**配置 `dr32_*_base*.json` 仍在 `configs/`:锚点臂 `drFlow.enabled=false`、
  不带 `version` 键 ⇒ 版本门本来就放行它们,移动会白白弄断路径。
  ⚠️ `pipeline/run_{web,corpus}_dr32_20260814.sh` 仍按旧路径引用这三个处理臂配置,**故意不改**
  —— 它们是收官 runner(冻结件),且批次实际跑的是各自 `_iso/` 快照里的副本。
  同一形状的先例:`student_sglang_web_{dr,base}_b302.json` 已退役而 `run_web_dr29_20260811.sh`
  至今仍引用旧路径。
