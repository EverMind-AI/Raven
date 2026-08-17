# `Raven-X/bench/` — 评测引擎

**2026-08-11 建**(Framework)。用户要求「RavenX 相关的内容文件都在同一个文件夹」——
评测引擎的**正文**因此从 `pipeline/` 搬到这里。

| 现在 | 原来 | 做什么 |
|---|---|---|
| `rollout.py` | `pipeline/04_rollout.py` | 跑 agent、落 `traj_raw.jsonl`、并发 / 看门狗 / 客户端检索账本 / 逐题工作区隔离 |
| `score.py` | `pipeline/06_validate.py` | **判分** —— exact / table / report 三个判官在这里分流 |

**名字里去掉了编号前缀。** 编号链其余 11 步(00–03、05、07–12)是数据合成时代的,
已进 `pipeline/legacy/`;只有这两步被改造成了评测引擎。继续叫 `04_`/`06_`
会让人以为还存在一条 00→12 的链,而那条链已经搁置。

---

## ★★★ 唯一真相源在这里,入口留在 `pipeline/`

```
pipeline/04_rollout.py   (640 B 转发器)  ─┐
pipeline/06_validate.py  (433 B 转发器)  ─┤→ pipeline/_bench_forward.py → 本目录
```

**入口没跟着搬**,因为 34 处调用点按 `04_rollout.py` 这个名字调它,其中 **6 个是冻结的
历史 runner**(已发表读数的复现记录 —— 改它们等于改「那批当时跑的是什么」),
还有 FutureX 的 `run_futurex.sh`。

⚠️ **两处各留一份完整代码是这里唯一不能选的形状。** runner 跑到的永远是 `pipeline/`
那个入口;若那里也有完整实现,本目录这份就**永远不会被执行**而又看起来是权威的 ——
它提供溯源的**外观**而非溯源本身。本项目已栽过同型:`pipeline/snapshot_source.py`
头部记着我当初在那里写了第二份 `tree_sha` 实现,同一棵树两个值,而症状恰好长得像
「代码真的变了」。

⇒ **任何行为改动都改这里。** `pipeline/` 那两个文件里不要加逻辑。

## 依赖仍在 `pipeline/`

`rollout.py` 需要 `common` / `eval_clean_adapter`,`score.py` 需要 `common` ——
这三个被 20+ 个 pipeline 脚本共用,搬过来会把破坏面扩大一整圈。
转发器用 `runpy.run_path` 而不是 `subprocess`,**继承调用方的 `sys.path`**
(其 `[0]` 就是 pipeline 目录)⇒ import 照旧解析,且**隔离下解析到的是快照里的
pipeline**,不是主线。`argv` / 环境变量 / 工作目录全部原样,`run_name="__main__"`
让 `if __name__ == "__main__"` 照常触发 ⇒ 对调用方零行为差异。

## 隔离:本目录被逐批快照

`pipeline/lib/isolate.sh` 把本目录复制进 `<批次根>/_iso/bench/`,
转发器在 `RAVEN_ISOLATED_FOR` 设着时**只认那份、找不到就响亮失败**。

★ 为什么不许静默回落主线:那会让这一条臂跑在「起批后被改过的」代码上,
而这种失败**没有症状**(分数照出、门照绿)。**一个会静默回落的转发器比没有隔离更坏。**

收批复验:`pipeline/check_isolation.py` 判据 **A2**(`bench_tree_sha`)。
它与 pipeline 的 sha 分开记 —— 两者不同源,合成一个数会让「谁变了」读不出来。
★ A2 是三条里最要紧的:`score.py` 直接决定分数,而 `tree_sha` 只覆盖 `raven/`
⇒ 它变了原本**没有任何门会响**。

## lint 豁免(有意的)

`bench/**` 在 `pyproject.toml` 的 `extend-exclude` 与 `.pre-commit-config.yaml` 的
`exclude` 里都开了口子。两条理由都量过:

- `ruff format` 会重写 **1,193 行** —— 落在 rollout 引擎和判分器上。它们决定每一条
  已发表读数的分数,1.2k 行重排买不到任何东西,还会让此后任何 diff 都读不出来。
- **516 行中文注释**是本项目的事故记录(工作区 `CLAUDE.md` 明写这几个文件保留中文
  约定、「不要统一」)。翻译过程本身会丢掉让它们值得保留的那部分。

⚠️ **更正(实测,别照抄我最初的说法)**:那 1,193 行重排**原本也不是全仓强制的**。
`make lint-python` 只扫 `PYTHON_LINT_TARGETS` 里的 6 个文件,从来没覆盖 `raven/` 或本目录;
仓里此刻已有 **28 个未格式化文件(其中 11 个在 `raven/`)** ⇒ 格式化不是既有保证。
真正会强加它的只有 pre-commit。⇒ **本豁免没有取消任何原本存在的保证**,但威胁面比我先前写的小。

⚠️ **豁免的生效范围是量出来的,不是假设的**:
`ruff` 的 `extend-exclude` 只在**发现模式**(`ruff check .`)下生效;传显式路径时它被忽略
(`ruff check bench/score.py` → 4 errors),要 `--force-exclude` 才恢复。
而 pre-commit **正是按显式文件名调 hook 的** ⇒ **承重的那层是 `.pre-commit-config.yaml`
的顶层 `exclude:`**,它在任何 hook 看到文件之前就过滤掉,与 ruff 旗标无关。
(同族前科:`check_cross_arm_completeness` 的臂名前缀 —— 回灌走直接调用、生产走
`acceptance_gate`,**一条从未在真实调用路径上验过的保险与不存在没有区别**。)

### 与 `AGENTS.md §1.2` 的关系(逐字读过原文后更正)

**不存在冲突。** §1.2 的**范围句**写得很明确:
「Repo source comments must not be in another language … **This constrains `raven/**` and
`tests/**`.**」—— `bench/` 两者都不是 ⇒ 它本来就不在 §1.2 的管辖内。
(我最初写的「需要一条正式例外」是**过度保守**,已更正:那句读的是首句的
"across the repo",而紧接着的范围句限定了它。)

⚠️ 但 §1.2 里有一句**位置描述已经过期**:
「The evaluation harness in `raven_train/pipeline/` is a separate tree with its own
Chinese-comment convention」—— 该 harness 的**引擎部分现在在本目录**。
这是一处需要更新的事实陈述,不是需要豁免的规则。**没有擅自改那份规则文件**
(AGENTS.md 是硬约束文件,且它自己写着「Before adding a section, confirm with the user
first」)⇒ 归用户裁决,建议把那句改成「… in `raven_train/pipeline/` and `Raven-X/bench/`」。

## 不在这里的东西

`Raven-X/tests/` 测 `raven/` 包(3,996 项)。本目录**不在** `raven/` 包内,
所以它既不进 `tree_sha`(⇒ **锚点指纹不受搬家影响**)也不进 wheel。
批次起批 / 验收 / 离线定价的脚本仍在 `pipeline/`,索引见 `pipeline/README.md`。
