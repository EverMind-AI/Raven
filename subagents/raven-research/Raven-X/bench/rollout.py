#!/usr/bin/env python3
#
# ═══════════════════════════════════════════════════════════════════════════
#  Stage ② rollout 引擎
#
#  20260811 Framework:内容从 `pipeline/04_rollout.py` 搬到这里 —— 用户要求「RavenX 相关
#  的内容文件都在同一个文件夹」。**这是唯一真相源**;`pipeline/04_rollout.py` 已改成
#  一层薄转发器(`runpy.run_path`),不是第二份实现。
#
#  ⚠️ 为什么不能是「复制」:runner 执行的入口是 `pipeline/04_rollout.py`,若两处各留一份
#     完整代码,跑到的永远是 pipeline 那份,而这里这份会静默漂移 ——
#     它提供的是溯源的**外观**而非溯源本身。本项目已栽过同型(`snapshot_source.py`
#     头部记的那次第二份 `tree_sha` 实现:同一棵树两个值,症状恰好像"代码真的变了")。
#
#  ★ 名字里去掉了编号前缀:编号链的其余 11 步已进 `pipeline/legacy/`(数据合成时代,
#    已搁置),只有 rollout 与 score 被改造成了评测引擎。带着 `04_`/`06_` 会让人
#    以为还存在一条 00→12 的链。
#
#  依赖:`common` / `eval_clean_adapter` 仍在 `pipeline/`(它们被 20+ 个 pipeline
#  脚本共用)。转发器用 `runpy` 执行本文件,**继承调用方的 sys.path**(其 [0] 就是
#  pipeline 目录)⇒ import 照旧解析,且隔离下解析到的是**快照里的** pipeline。
#
#  ⚠️ 本文件保留 pipeline 的中文注释约定(CLAUDE.md 明写「不要统一」)——
#     那些注释是事故记录。`bench/**` 已在 pyproject / .pre-commit 里开 lint 豁免;
#     AGENTS.md §1.2(comments in English)需要一条正式例外,**未擅自改那份规则文件**。
# ═══════════════════════════════════════════════════════════════════════════
"""Stage ② Rollout (deepseek-v4-pro @ EverClaw) → data/traj_raw.jsonl

For each seed task, shell out to the Raven CLI with the teacher config
(默认 configs/teacher_glm.json = GLM-5.2-NVFP4@Volc, temp 1.0, reasoning high,
maxToolIterations **150** —— see DEFAULT_CONFIG). 审计 §2.3:cap 太低(48)会让
~38 条 OpenSeeker 撞顶→合成兜底以 status=ok 静默混入,故默认用高 cap 配置。Secrets
(DEEPSEEK_API_KEY / SERPER_API_KEY / 网关 key) are injected into the subprocess
env — never written to disk in this folder.

  everclaw agent -m <question> -w <ws> --no-markdown --no-logs --config <cfg>

The web_search tool hits Serper (X-API-KEY from env); web_fetch hits Jina.
We read back the session JSONL EverClaw persists at <ws>/sessions/cli/<id>.jsonl,
extract the trajectory + final answer + tool metrics + evidence URLs, and log
search-health signals (§1.3). Resumable: (qid, sample_id) already present skip.

NOTE (pilot scope): a true read-through cache (§1) is a proxy-level production
component; at --samples 1 there is ~no cross-sample query overlap, so we record
the SERP/page content that already lands in the trajectory as the evidence
record and leave the caching proxy out. See README.

Usage: python 04_rollout.py [--limit N] [--samples K] [--concurrency C]
                            [--timeout S] [--only QID[,QID...]] [--config PATH]
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import json
import os
import re
import shutil
import time
import urllib.request
from collections import Counter
from pathlib import Path

from common import (BROWSE_TOOLS, DATA, EVERCLAW, SRC, TEACHER_MODEL,
                    evidence_text, read_jsonl, render_runtime_config,
                    rollout_env, teacher_chat, textof, write_jsonl)

# No-tools finalizer (§ Kimi finalization fix): a rollout that gathered evidence
# but never emitted a final answer (status=no_answer) is salvaged with ONE tool-less
# teacher call over the evidence it already fetched, appended as the terminal turn.
_FINALIZE_SYS = (
    "You are finalizing a research task. Based ONLY on the research evidence gathered "
    "below, give your best final answer to the question. Do NOT call any tools and do "
    "NOT ask for more. If evidence is insufficient, answer with your best-supported "
    "candidate and note what is uncertain. Put the final answer inside <answer></answer>."
)
_FINALIZE_SYS_ZH = (
    "你在为一个研究任务收尾。仅依据下面已收集到的研究证据,给出对问题的最终答案。"
    "不要调用任何工具、不要再索取信息。若证据不足,就给出最有依据的候选答案并说明不确定处。"
    "把最终答案放进 <answer></answer>。"
)


# ★ dr@2.3:答案可见性判据**只有一个实现**,在 eval_clean_adapter 里。这里 import 而不重写。
# 导入失败时退化成"永远可见"并在字段里说明 —— **不能静默换一把尺子**,那正是本次要消灭的。
try:
    from eval_clean_adapter import extract as _clean_extract
    from eval_clean_adapter import salvage_committed as _salvage_committed
    _VIS_SRC = "eval_clean_adapter.extract"
except Exception as _exc:  # pragma: no cover
    _VIS_SRC = f"(unavailable: {type(_exc).__name__}) —— answer_visible 不可信"

    def _clean_extract(a, exempt_closing_tag=False):  # type: ignore[misc]
        return a or ""

    def _salvage_committed(_row):  # type: ignore[misc]
        return False


@functools.lru_cache(maxsize=8)
def _caps(config_path: str) -> tuple[int, int]:
    """(单轮生成上限 tokens, 迭代上限) —— 从**渲染后**的运行配置读,不硬写。

    ★ dr@2.3:`answerless_cause` 要区分"撞单轮生成上限"和"真的停下来了",而那条线就是
    `agents.defaults.maxTokens`。硬写 16384 会在有人调旋钮时静默失准,而**失准的方向是把
    撞上限误判成 unclosed_think**,即把一个预防型靶子藏进一个不可修的桶。
    `runtime_config` 传进来的是**路径**(`render_runtime_config` 的返回值),不是 dict ——
    第一版我按 dict 用了它,这个 helper 就是为了让那个错不可能再犯。
    """
    gen_cap, iter_cap = 16384, 150
    try:
        d = json.loads(Path(config_path).read_text(encoding="utf-8"))
        gen_cap = int(((d.get("agents") or {}).get("defaults") or {}).get("maxTokens") or gen_cap)
        _dr = d.get("drFlow") or {}
        iter_cap = int(_dr.get("maxIterations") or (d.get("agents") or {}).get("maxIterations")
                       or iter_cap)
    except Exception:
        pass
    return gen_cap, iter_cap


def _final_turn_end(traj: list) -> dict:
    """末条 `turn_end` observer 的负载,取不到就 `{}`。

    ★ 20260813 新增。`answerless_cause` 判"撞没撞单轮生成上限"时拿的是**名义** cap
    (`agents.defaults.maxTokens`),而真正在最后那次调用上生效的是 `_fit_request` 可能
    已经下调过的 reserve —— 无损优先策略在上下文涨起来时会主动降它,并且把结果作为
    `final_completion_cap` 逐题落在这个 observer 里。两个数在深轮次上差一个数量级:
    实测 `eval_web_dr30_20260812/run_dr` 有 9 题的有效 cap 是 2,192–15,457,名义 16,384。

    ⇒ 用名义值去比字符数,会把"写到自己真实预算为止"判成"没闭合 think 标签",
    而 `_caps` 的 docstring 早就写明了这个误判**方向固定**:「宁可少认一条预防型靶子」。
    这次实测到的就是那个方向 —— dr 侧 9/15 条 `length` 截断被藏进了不可修的桶。
    有效 cap 一直就在同一个 observer 里,隔了二十行。
    """
    out: dict = {}
    for t in traj or ():
        if isinstance(t, dict):
            te = (t.get("observers") or {}).get("turn_end")
            if isinstance(te, dict):
                out = te
    return out


async def finalize(seed: dict, traj: list, config_path: str) -> str:
    """Tool-less finalize call over the gathered evidence. Returns answer text
    ('' when there's no evidence or the call fails → caller keeps no_answer)."""
    ev = evidence_text(traj, 30000)
    if not ev.strip():
        return ""
    sys = _FINALIZE_SYS_ZH if seed.get("lang") == "zh" else _FINALIZE_SYS
    msgs = [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"Question:\n{seed['question']}\n\n"
         f"Research evidence you gathered:\n{ev}\n\nNow give your final answer in <answer></answer>."},
    ]
    return (await asyncio.to_thread(teacher_chat, config_path, msgs, 4096, 0.3, False)).strip()

SEEDS = DATA / "seed_tasks.dedup.jsonl"
SEEDS_FALLBACK = DATA / "seed_tasks.jsonl"

# §6.5 tool-collapse fix: force the teacher to SEARCH before answering. OpenSeeker
# puzzles otherwise make the model ruminate from memory (search=0) and, with
# reasoning=high, overrun the wall before the first turn even commits.
# RQ1-05 修复:nudge 语言随题走 —— 旧版对所有题注入中英双语(中文段 priming 出
# 8/26 条英文题中文作答);现按 seed.lang 只给对应语言的前导。
SEARCH_FORCING_PREFIX_EN = (
    "IMPORTANT — You MUST research before answering. Use the web_search tool "
    "FIRST to find candidates, then web_fetch the most relevant page(s) to "
    "verify. Do NOT answer from memory or internal knowledge. Keep your "
    "reasoning brief and act: issue your first web_search now.\n\n"
    "Task:\n"
)
SEARCH_FORCING_PREFIX_ZH = (
    "【重要】回答前必须先检索:先用 web_search 找候选,再 web_fetch 打开最相关页面核实;"
    "禁止凭记忆/已有知识直接作答;少想多做,现在就发起第一次 web_search。\n\n"
    "Task:\n"
)

# Anti-overthinking variant: also caps the rumination loop that hangs the hardest
# zh puzzles (model re-derives the whole riddle forever, first turn never commits).
# §1 (优化方案) forced finalize: when the budget is reached OR enough evidence is
# gathered, the model must IMMEDIATELY emit its final answer in <answer></answer>
# and STOP — and must NOT dump its step-by-step reasoning into that final answer
# (the clean stage's hard gate is the backstop, this is the upstream prevention).
ANTI_OVERTHINK_PREFIX_EN = (
    "IMPORTANT — Research, don't ruminate. Use web_search FIRST and base your "
    "answer ONLY on what you find; do NOT answer from memory. Think BRIEFLY "
    "between steps. Do NOT re-derive or re-analyze the whole question "
    "repeatedly. Use AT MOST 8 web_search calls; web_fetch the most relevant "
    "pages to verify. If still unsure after ~8 searches, STOP and answer with "
    "your single best-supported candidate. Never loop. When you have enough (or "
    "hit the search limit), FINALIZE NOW: output ONLY your final answer wrapped "
    "in <answer></answer> and stop — do NOT paste your reasoning/thinking into "
    "the final answer.\n\n"
    "Task:\n"
)
ANTI_OVERTHINK_PREFIX_ZH = (
    "【重要】要检索、别空想。先用 web_search,只基于检索结果作答,禁止凭记忆。每步只"
    "简短思考,绝不反复重新分析整道题。最多搜索 8 次,用 web_fetch 打开最相关页面核实;"
    "搜约 8 次后仍不确定就停止,给出最有依据的单一最佳答案,绝不空转循环。一旦信息足够"
    "(或到达搜索上限)立即收尾:只输出 <answer></answer> 包裹的最终答案并停止,"
    "禁止把思考过程粘进最终答案。\n\n"
    "Task:\n"
)

# 病根验证 (2026-07-14): 纯 fetch-forcing 变体 —— 只强制"每次 search 后必须 fetch
# 正文",不限搜索次数、不促收尾,以隔离"检索-消化链坍缩(搜了不 fetch→信息真空→循环)"
# 这一单一变量。用于深研组 80 题的病根因果验证,勿与 anti_overthink(混合干预)混用。
FETCH_FORCING_PREFIX_EN = (
    "IMPORTANT — After EVERY web_search, you MUST web_fetch at least one of the "
    "result pages and READ its full content BEFORE your next step or answer. Do "
    "NOT decide or answer from search-result snippets or memory alone — always "
    "open and read the source page first.\n\n"
    "Task:\n"
)
FETCH_FORCING_PREFIX_ZH = (
    "【重要】每次 web_search 之后,必须先用 web_fetch 打开至少一个结果页面、读取正文,"
    "再进行下一步或作答;禁止只凭搜索摘要或记忆判断——务必先打开并阅读来源页面。\n\n"
    "Task:\n"
)


# W2 认输课前缀(v4.1,需求#1 最高优先):坚持检索到预算(~20 次,W3 实测 60K 窗
# 封顶 ~22),预算尽仍无解 → 给最有依据的单一候选 + 明示不确定与已排除项,绝不循环。
# 格式守 10 号 _NUDGE_RE 约定(IMPORTANT — /【重要】…\n\nTask:\n)→ 自动剥离。
PERSIST_CONCEDE_PREFIX_EN = (
    "IMPORTANT — Persist, then conclude honestly. Use web_search FIRST and base "
    "everything ONLY on what you find. Do NOT give up early: keep searching from "
    "different angles and web_fetch the most relevant pages, up to about 20 "
    "searches. If after that budget you still cannot pin down the answer, STOP "
    "and finalize honestly: give the single best-supported candidate (or state "
    "that every candidate was ruled out), say clearly that the conclusion is "
    "uncertain and what you ruled out. NEVER pad, repeat yourself, or loop. "
    "Wrap the final answer in <answer></answer> and stop.\n\n"
    "Task:\n"
)
PERSIST_CONCEDE_PREFIX_ZH = (
    "【重要】先坚持,后诚实收尾。先用 web_search,一切只基于检索结果。不要过早放弃:"
    "从不同角度持续检索并用 web_fetch 打开最相关页面核实,最多约 20 次搜索。若预算用尽"
    "仍无法锁定答案,立即停止并诚实收尾:给出最有依据的单一候选(或说明所有候选均被"
    "排除),明确声明结论不确定、说明已排除了什么。绝不灌水、不重复、不循环。"
    "最终答案用 <answer></answer> 包裹后停止。\n\n"
    "Task:\n"
)

# W3 链长课程前缀:反早收敛(442 学了"早收敛"在超长链题上反成短板)——教
# "预算内坚持检索+读正文"的正样本;预算尽的收尾行为与认输课同款(两课一体两面)。
PERSISTENCE_PREFIX_EN = (
    "IMPORTANT — Do not settle early. This task needs MANY searches to resolve: "
    "verify EVERY constraint in the question with its own web_search, and "
    "web_fetch the key pages to confirm details — do not answer from snippets or "
    "memory. Only finalize once every constraint is verified against fetched "
    "sources (typically 12-20+ searches), or your search budget (~20) is "
    "exhausted — then give the best-supported answer, noting any unverified "
    "constraint. Never loop or repeat. Wrap the final answer in "
    "<answer></answer> and stop.\n\n"
    "Task:\n"
)
PERSISTENCE_PREFIX_ZH = (
    "【重要】不要过早下结论。这道题需要多次检索才能解决:题面每个约束都要单独 "
    "web_search 验证,并用 web_fetch 打开关键页面核对细节——禁止只凭摘要或记忆作答。"
    "只有当所有约束都对照抓取到的来源验证过(通常需要 12-20 次以上搜索),或搜索预算"
    "(约 20 次)用尽时才收尾——此时给出最有依据的答案,并注明未能验证的约束。"
    "绝不循环、不重复。最终答案用 <answer></answer> 包裹后停止。\n\n"
    "Task:\n"
)


def pick_prefix(mode: str, seed: dict, sample_id: int, nudge_prob: float) -> str:
    """按题选前导:语言随题走(RQ1-05);nudge_prob<1 时按 (qid,sample_id) 确定性
    随机剥离部分前导(PROTO-06:26/26 全带前导会让学生学出'有前导才搜'的条件依赖;
    对 browse 率已塌方的学生,搜索行为必须无条件化。建议放量取 0.6~0.8)。
    确定性哈希保证 resume 复现同一决定。"""
    if not mode:
        return ""
    if nudge_prob < 1.0:
        h = int(hashlib.md5(f"{seed['qid']}#s{sample_id}".encode()).hexdigest(), 16)
        if (h % 1000) >= int(nudge_prob * 1000):
            return ""
    zh = seed.get("lang") == "zh"
    if mode == "fetch_forcing":
        return FETCH_FORCING_PREFIX_ZH if zh else FETCH_FORCING_PREFIX_EN
    if mode == "anti_overthink":
        return ANTI_OVERTHINK_PREFIX_ZH if zh else ANTI_OVERTHINK_PREFIX_EN
    if mode == "persist_concede":      # W2 认输课
        return PERSIST_CONCEDE_PREFIX_ZH if zh else PERSIST_CONCEDE_PREFIX_EN
    if mode == "persistence":          # W3 链长课程
        return PERSISTENCE_PREFIX_ZH if zh else PERSISTENCE_PREFIX_EN
    return SEARCH_FORCING_PREFIX_ZH if zh else SEARCH_FORCING_PREFIX_EN
RAW = DATA / "traj_raw.jsonl"

# ── 工作区路径本身就是写给模型的提示 ──────────────────────────────────────────
# `Raven-X/raven/context_engine/segments/render.py` 的 identity 段把工作区**绝对
# 路径**原样写进 system prompt("## Workspace / Your workspace is at: {path}",还
# 附三条子路径)。旧命名 `<batch>/run_<arm>/workspaces/<qid>_s<n>` 因此一次泄露三样:
#   ① 题库名(路径里的 browsecomp / widesearch)② 题号 ③ **臂名**。
# 实测(T2c web 根):`run_base` 的 browsecomp-13 题面里既没有 "browsecomp" 也没有
# "13",它却发了 17 次含 browsecomp 的检索并去 fetch Kaggle/HF 上的 BrowseComp
# 数据集;`run_base_rep` 的 ws_zh_014 直接 fetch 了 gold 表格 CSV(文件名从 qid
# 推出来的);还有一题 fetch 了自己工作区里的 episodes.md —— 那个路径只能从这行
# prompt 学到。触顶率按臂不同(base 12 / base_rep 10 / matched 7 / dr14 4 / dr16 5)
# ⇒ 这是**按臂相关的污染源**,事后剔题系统性不中立。臂名进 prompt 尤其致命:
# A−B 设计里唯一不能让模型知道的就是"我在哪条臂上"。
# 修法 = 路径每一段换成不可逆哈希、根目录名中性。物理隔离一分不减(每题每臂仍是
# 独立目录),只是名字不再可读。qid↔哈希对照表落在 rundir(在工作区之外,模型够不到)。
def _tok(s: str, n: int = 8) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:n]


_BATCH_TOK = _tok(DATA.parent.name)   # 批次目录名 → 8 hex
_ARM_TOK = _tok(DATA.name)            # run_<arm>  → 8 hex
# _ws 挂在 `runs/` 下,不挂在题集根下:题集根叫 `eval_targets_quick_v1_corpus`,
# 那串字既告诉模型"你在被评测",又告诉它这条轴是语料还是 live web。它不按臂变化
# (不是 A−B 的偏置源),但白送的提示没有留着的理由。
_RUNS = next((p for p in DATA.parents if p.name == "runs"), DATA.parent)
WS_ROOT = Path(os.environ.get("PILOT_WS_ROOT")
               or (_RUNS / "_ws" / _tok(DATA.parent.parent.name) / _BATCH_TOK / _ARM_TOK))
WS_MAP = DATA / "ws_map.jsonl"        # {qid, sample_id, ws_token, ws} —— 离线回联用
CFG_ROOT = DATA / "_cfg"              # 逐题派生配置(同样在工作区之外)


def per_question_config(config: str, qtok: str, sample_id: int) -> str:
    """给语料臂的 `corpusEndpoint` 追加 `/r/<armtok>__<qtok>`,让 BM25 服务的检索
    日志能**逐题**归属。动机:语料轴上有 21 道题说不清是"gold 从没在检索结果里
    露头"还是"露头了但被上下文省略抹掉",而这两种情况指向完全不同的修法(补检索
    vs 补证据保留),现在的日志只记到臂级、判不出来。
    token 用哈希不用 qid:它会出现在 HTTP URL 里,而工具的错误文本可能把 URL 回显
    给模型 —— 那就把刚堵上的题号泄露从 prompt 挪到了工具返回里。

    ★ 20260803 修:原先只拼 `_ARM_TOK`,而 `_ARM_TOK` 只哈希 run 目录名(`run_base`)⇒
    **臂名跨批次重复的臂,token 就跨批次重复**。实测碰撞:`run_base` 与 `run_base_rep`
    各 5 个批次共用一个 token(`fdbb1ba3` / `b63b09c3`)、`run_official_matched` 3 个。
    而 dr17/dr19/dr20/dr21 每批名字不同 ⇒ token 唯一、日志干净。也就是说这个缺陷
    **精确地只污染锚点臂和外部基线** —— 它是按臂相关的仪器污染,不是随机噪声:任何
    "gold 在检索结果里露头率"之类从服务端 /r/ 日志算出来的量,锚点侧是 5 批的并集,
    处理侧是单批。`WS_ROOT`(:229)当时就带上了 `_BATCH_TOK`,只有这条端点漏了。
    (不受影响的是 `pipeline/replay_serp.py`:它离线重放 BM25,不读服务端日志。)"""
    try:
        cfg = json.loads(Path(config).read_text())
    except Exception:
        return config
    web = (cfg.get("tools") or {}).get("web") or {}
    cep = web.get("corpusEndpoint")
    if not cep:
        return config
    base = cep.rstrip("/").split("/r/")[0]      # 幂等:已带 /r/<token> 的不叠加
    cfg["tools"]["web"]["corpusEndpoint"] = f"{base}/r/{_BATCH_TOK}_{_ARM_TOK}__{qtok}"
    CFG_ROOT.mkdir(parents=True, exist_ok=True)
    out = CFG_ROOT / f"{qtok}_s{sample_id}.json"
    out.write_text(json.dumps(cfg, ensure_ascii=False))
    return str(out)


LEDGER_ROOT = DATA / "_web_ledger"    # 客户端检索账本(逐题一文件),在工作区之外


def _ledger_health() -> dict:
    """从**客户端检索账本**重算工具面健康度。空账本(没开)⇒ 返回空 dict。

    ## 为什么必须有这一份,轨迹侧那份不够

    轨迹侧是**事后**从最终 trajectory 上数的,而失败的工具结果会被 elide 成
    `[earlier tool output elided …]` 占位符 —— 错误文本没了,于是数不到。
    dr@2.4 web 轴实测这个低估**很大而且按臂相关**:

        逐臂 fetch 失败      轨迹侧数到      账本真值
        base                492 / 1,059      1,059 / 1,059  (低估 53%)
        dr24              2,953 / 8,129      8,128 / 8,128  (低估 64%)

    dr 侧上下文更大 ⇒ elide 更多 ⇒ **低估更狠**。任何从轨迹算的工具面率因此都
    系统性偏向"dr 看起来更健康",方向固定。账本**在事件发生那一刻落盘**,不事后反解。

    ⚠️ 它自己的盲区要一并说清:账本只记**到达工具层**的调用。模型没发起的调用、
    被 denylist 在工具层之前挡掉的调用,这里都看不见(`exec` 被拒那 24 次就只在
    轨迹里有)。所以两份并列报,不互相替代。
    """
    if not LEDGER_ROOT.exists():
        return {}
    n_s = n_f = n_f_ok = n_replay = n_retry = 0
    n_outcome = n_terr = n_zero = n_ab = n_kg = n_sn = n_shape = 0
    unknown_ops: Counter = Counter()
    retry_errs: Counter = Counter()
    errs: Counter = Counter()
    for fp in LEDGER_ROOT.glob("*.jsonl"):
        try:
            for line in fp.open():
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                op = r.get("op")
                if op == "search":
                    n_s += 1
                    if r.get("replay"):
                        n_replay += 1
                    # dr@2.7:三件事不再压在一个 `failed` 布尔里。实测四臂
                    # `failed ⟺ n==0` 零例外 ⇒ 它几乎全是**零命中查询**(行为),
                    # 真传输错误差三个数量级。⚠️ 老批次没有这些键 ⇒ 用 `n_outcome`
                    # 显式记"测没测量",缺键报 None 而不是 0(缺数据 ≠ 0 分)。
                    if "transport_err" in r:
                        n_outcome += 1
                        n_terr += bool(r.get("transport_err"))
                        n_zero += bool(r.get("zero_hit"))
                    # ⚠️⚠️ 20260807:这三行原先是**无条件**的 ⇒ 缺键贡献 0,然后被
                    # 除、被四舍五入、被印成一个测量值。实测把它跑在 20260806 那批
                    # (base 的 748 行完全没有这三个键)上,它打印
                    # `ledger_serp_*_chars_per_search: 0.0` —— 与 20260807 那批
                    # **真的测到 0.0** 的 DR 臂逐字节相同。而这条轴上 answerBox 的
                    # 真值就是 0.0(Serper 对 DRB 这类长开放问题一次都没返回答案框)
                    # ⇒ **伪装是完全的**:缺键的 0.0 与测到的 0.0 在事实上不可区分。
                    # ★ 正确写法就在上面 12 行(`n_outcome`)—— 我写对了一次,紧接着
                    #   在下面写错了同一件事。⇒ 同一份代码里已有正确范式时,新增的
                    #   聚合必须照抄它,而不是重新想一遍。
                    # 另:分母也必须换成**带键的行数**。分子只可能来自带键的行,分母若
                    # 用全部 search 行,混合 schema 的批次会被稀释向 0 —— 实测两批 base
                    # 合起来(1362 行,其中 614 行带键)会把 snippet 报成 514.5,真值
                    # 是 1141.3,**稀释 2.22 倍**。而 rollout 是可续跑的(`done_keys()`
                    # 跳过已完成),所以跨 dr@2.7 边界续跑的臂真的会混 schema。
                    if "answer_box_chars" in r:
                        n_shape += 1
                        n_ab += int(r.get("answer_box_chars") or 0)
                        n_kg += int(r.get("knowledge_chars") or 0)
                        n_sn += int(r.get("snippet_chars") or 0)
                elif op == "fetch":
                    n_f += 1
                    if r.get("ok"):
                        n_f_ok += 1
                    else:
                        e = str(r.get("error", ""))
                        # 归一化成"错误族",否则每条 URL 各成一类、看不出模式
                        m = re.search(r"'(\d{3}) ([^']+)'", e)
                        errs[m.group(0) if m else e[:48]] += 1
                # ★★★ 重试必须单独记,否则这份体检会被自己的修复弄瞎:
                # dr@2.7 之前一次阅读器故障表现为 fetch_ok_rate 塌下去(那批就是这么
                # 抓到的);加了重试之后,同一次故障会被悄悄重试掉、ok_rate 回到 ~1.0,
                # 判据 B 再也不开火 —— 修复删掉了门赖以开火的那个信号。重试率是
                # **环境强加**的(不是臂自己的行为),所以它属于 D1 硬判据那一类。
                elif op in ("search_retry", "fetch_retry"):
                    n_retry += 1
                    e = str(r.get("error", ""))
                    m = re.search(r"'(\d{3}) ([^']+)'", e)
                    retry_errs[m.group(0) if m else e[:48]] += 1
                else:
                    # 本读数器只认它列举过的 op。dr@2.7 起 verify gate 也往同一本账本
                    # 写(`verify` / `verify_gate`),而这份体检若静默丢掉它们,就正好是
                    # "什么事件在结构上不会到达你"那条法则:明天有人加一个 op,这里
                    # 既不会数它,也不会说它存在。
                    unknown_ops[str(op or "(缺 op)")[:24]] += 1
        except OSError:
            continue
    if not (n_s or n_f):
        return {}
    return {
        "ledger_searches": n_s,
        "ledger_replay_rate": round(n_replay / n_s, 3) if n_s else None,
        "ledger_fetches": n_f,
        # ★ 这是本份里最该看的一个数:它为 0 就意味着这批**读不了任何网页**。
        "ledger_fetch_ok_rate": round(n_f_ok / n_f, 3) if n_f else None,
        "ledger_fetch_err_top": dict(errs.most_common(5)),
        # 分母是"逻辑调用数"(重试不计入调用),所以这个比率读作
        # "每 100 次调用里有多少次需要重发",可以 >1。
        # ★ 搜索面的**真**错误率(不含零命中)。`ledger_search_outcome_n` 为 0 ⇒
        # 这批没测量,不是零错误。20260806 的规矩是工具面率一律从账本算,而账本里
        # 此前唯一像错误的字段是 `failed` —— 它几乎全是零命中,拿它当错误率会给
        # "锚点故障 18.9%"的假红灯。
        "ledger_search_outcome_n": n_outcome,
        "ledger_search_transport_err_rate":
            round(n_terr / n_outcome, 5) if n_outcome else None,
        "ledger_search_zero_hit_rate":
            round(n_zero / n_outcome, 4) if n_outcome else None,
        # SERP 整形的注入量级(字符/次检索)。锚点 flow off ⇒ 三个旋钮都开,DR 臂关掉。
        # ⚠️ 但"开着"不等于"有内容":报告轴上实测 answerBox 与 KG 在锚点的 614/614 行
        # 上都是 0.0(Serper 对这类长开放问题不返回答案框),只有 snippet 带内容
        # (1141.3 字符/次)⇒ **有效旋钮个数是个经验量,要逐批从账本读,不能从配置推。**
        # ★ `ledger_serp_shaping_n` 是**存在性分母**:它为 0 ⇒ 这批账本没有这三个键
        #   ⇒ 三个率报 None(**未测量**),不是 0.0。
        "ledger_serp_shaping_n": n_shape,
        "ledger_serp_answerbox_chars_per_search":
            round(n_ab / n_shape, 1) if n_shape else None,
        "ledger_serp_knowledge_chars_per_search":
            round(n_kg / n_shape, 1) if n_shape else None,
        "ledger_serp_snippet_chars_per_search":
            round(n_sn / n_shape, 1) if n_shape else None,
        # 混合 schema 的显式披露:两者不等 ⇒ 上面三个率只描述其中一部分行。
        "ledger_serp_shaping_mixed_schema": n_shape != n_s,
        "ledger_unknown_ops": dict(unknown_ops.most_common(6)),
        "ledger_retries": n_retry,
        "ledger_retry_rate": round(n_retry / (n_s + n_f), 4) if (n_s + n_f) else None,
        "ledger_retry_err_top": dict(retry_errs.most_common(5)),
    }


def web_ledger_env(env: dict, qtok: str, sample_id: int) -> dict:
    """派生本题专用 env,注入**客户端**检索账本路径。**绝不 mutate 传进来的 env。**

    为什么必须是客户端账本(不是把语料那套搬过来):语料轴靠的是**我们自己的** BM25
    服务端日志,而 web 轴是 live Serper —— 没有我们控制的服务端。更要紧的是,服务端
    账本有一个**结构性盲区**:`repeat_notice` 的缓存命中**定义上就是"请求不到达服务端"**
    (实测 dr23 11,081 次调用 vs 服务端账本 6,857 ⇒ **38.1% 是重放**;base 因
    `repeat_notice=False` 实测 0 次、比值恒 1.00 ⇒ 该测量自带正控)⇒ 只有在客户端记
    才看得见重放。**所以它不是移植,是比语料轴那套更强的仪器** —— 前提是重放位在**发生
    的那一刻**落盘,而不是事后从渲染过的轨迹里重建("某一跳发生了什么必须由 run 自己
    作为一等产物字段落盘")。

    ★ **一题一文件,文件名 = `<批次>_<臂>__<题>_s<样本>`**,与 `WS_ROOT` 下工作区目录名
    同构(`candidate_tags()` 已经会 `split("_s")[0]`)⇒ ① 16-48 个并发进程各写各的,
    **结构上不可能交错**(共享一个文件就要赌小写原子性);② 归属直接从文件名读,
    不必事后重建;③ k>1 时同题不同样本也不撞。
    ⚠️ `env` 被全部并发题共用 ⇒ 必须 copy;改一处就是全批污染。
    ⚠️ 它对**所有臂**无条件开(含锚点与语料臂):它只写不读、不进模型上下文 ⇒ 不改分布、
    分数预算 0,而语料臂上多一份客户端账本正好给"重放占比"提供第一手交叉核对。
    """
    out = dict(env)
    LEDGER_ROOT.mkdir(parents=True, exist_ok=True)
    out["RAVEN_WEB_LEDGER"] = str(
        LEDGER_ROOT / f"{_BATCH_TOK}_{_ARM_TOK}__{qtok}_s{sample_id}.jsonl")
    return out
# 2026-07-06 团队定案:生产教师 = Kimi-K2.7-code @ Volc 网关(成本控制;免密内网,
# reasoning 经 `reasoning` 字段透传、Raven 双键兼容已实弹验证)。默认即生产配置;
# 对照实验用 --config 显式指定其他教师。收尾不稳 → 生产跑法必须带 --finalizer。
DEFAULT_CONFIG = SRC / "configs" / "teacher_glm.json"


def read_session(ws: Path) -> list[dict]:
    sess_dir = ws / "sessions"
    if not sess_dir.is_dir():
        return []
    # this EverClaw build writes sessions/cli_direct.jsonl (single-message mode);
    # older builds used sessions/cli/<id>.jsonl — glob recursively to cover both.
    files = sorted(sess_dir.rglob("*.jsonl"))
    if not files:
        return []
    path = max(files, key=lambda p: p.stat().st_size)
    msgs = []
    for line in path.open():
        line = line.strip()
        if line:
            try:
                msgs.append(json.loads(line))
            except Exception:
                pass
    return msgs


def read_harness_error(ws: Path) -> str | None:
    """分类"harness 把这一题弄死了",而不是"模型没给答案"。

    A' 批次(07-24)实锤:B 臂 66/440(15.0%)撞 context-window 400 直接终局,
    Raven 按设计**不把报错轮写进 session**(免得毒化上下文),于是 traj 里最后
    一条 assistant 是上一轮 tool-call(内容=纯 reasoning),analyze() 拿它当
    final_answer、判官按空答判 0 分 —— 全程 status=ok / rc=0,**静默**。
    这些题判对率恒 0,但归因方向完全不同(harness bug ≠ 模型能力),必须分开记。

    读 curator trace 的 main_agent_result.final_content(每个 workspace 都有,
    与 memory backend 是否开无关);轨迹被外部 kill 的题没有这条 → None。
    """
    for f in sorted(ws.glob("memory/.curator/traces/*/*.jsonl")):
        try:
            lines = f.read_text(errors="replace").strip().split("\n")
        except OSError:
            continue
        for line in reversed(lines):
            if '"main_agent_result"' not in line:
                continue
            try:
                payload = json.loads(line).get("payload") or {}
            except Exception:
                continue
            fc = ((payload.get("response") or {}).get("final_content") or "")
            if "maximum context length" in fc or "context_length_exceeded" in fc:
                return "context_overflow"
            if fc.startswith("Error calling LLM") or fc.startswith("Sorry, I encountered an error"):
                return "llm_error"
            if "no response to give" in fc:
                return "no_response_dud"
            return None
    return None


def analyze(trajectory: list[dict]) -> dict:
    tool_calls, final_answer = [], None
    n_assistant = 0
    n_search = n_fetch = n_empty_search = n_search_err = 0
    n_fetch_err = n_other_err = 0
    id2name: dict[str, str] = {}          # tool_call_id → 工具名(逐工具归错必需)
    evidence_urls, search_queries = [], []
    for m in trajectory:
        role = m.get("role")
        if role == "assistant":
            n_assistant += 1
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                name = fn.get("name")
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                tool_calls.append({"name": name, "arguments": args})
                if tc.get("id"):
                    id2name[tc["id"]] = name
                if name == "web_search" and isinstance(args, dict):
                    n_search += 1
                    search_queries.append(args.get("query", ""))
                elif name == "web_fetch" and isinstance(args, dict):
                    n_fetch += 1
                    if args.get("url"):
                        evidence_urls.append(args["url"])
            c = textof(m.get("content")).strip()
            if c:
                final_answer = c
        elif role == "tool":
            tc = textof(m.get("content"))
            # ⚠️⚠️ **`n_empty_search` 不可用于零结果统计。** 20260813 钉死这条口径。
            # 它数的是**轨迹里含某个特定文案的 tool 消息**,而不是"返回零命中的检索":
            #   ① 被 elide 掉的 tool 结果内容已经不在了 ⇒ 这个字符串检查看不见它们;
            #   ② 重放行与被规则拒发的行文案不同,也数不进来;
            #   ③ "No results for" 只是其中一种措辞。
            # 实测 eval_web_dr30_20260812:本字段 131,而客户端账本的 zero_hit 是 592
            # (**差 4.5×**;`bc-739` 是 0 vs 13)。
            # ⇒ 零结果相关的任何定价一律走 `_web_ledger` 的 `zero_hit` 字段。
            # 本字段保留不改口径 —— 已发表批次按它的旧含义读它(与 `failed`/`k` 同规矩)。
            if "No results for" in tc:
                n_empty_search += 1
            # 审计第一档#1:旧检测只认两个 Serper 特定文案 → p1 时代真实报错
            # ~17-40% 却记 0.0,§1.3 静默污染告警永不触发。改为通用错误头识别
            # (Raven 工具错误一律以 "Error: "/"Proxy error:" 开头)。
            # PROTO-06d(8维审计 P0,同一检测第二次静默失效):Raven 现把工具结果包在
            # [BEGIN UNTRUSTED …] 防注入头里,错误头落在第二行 → 锚定首行的 re.match
            # 恒不中,n_search_err 又归 0。先剥头再判;另补 web_fetch 的 {"error":…}
            # JSON 错误形态。
            body = tc
            mh = re.match(r"\s*\[BEGIN UNTRUSTED [^\]]*\]\s*\n?", tc)
            if mh:
                body = tc[mh.end():]
            if re.match(r"\s*(Error:|Proxy error:)", body[:100]) \
                    or body.lstrip()[:12].startswith('{"error"') \
                    or "Serper API key not configured" in tc:
                # 20260806(Framework):**按工具归错**。旧代码把任何工具的错误都记进
                # `n_search_err`,而 health 又拿它除以 `n_search` —— 分子是"全部工具的
                # 错误数"、分母只是"搜索次数",于是这个门量的不是它名字说的东西
                # (本项目第六次同形)。dr@2.4 web 轴实测:名义 8.5% 里真正的搜索错误
                # 只有 17/35,139 = 0.048%,其余全是 `web_fetch` 打在 r.jina.ai 上的
                # 402 —— 诊断因此完全指错了方向(读成"Serper 断粮",真相是阅读器欠费)。
                # ⚠️ 保留 `n_search_err` 这个键名不变,但语义收窄为"web_search 的错误";
                # 跨版本比较必须知道这件事(旧值偏大,且偏大的幅度与 fetch 次数相关
                # ⇒ **按臂相关**,dr 侧 fetch 更多 ⇒ 旧口径系统性冤枉 dr)。
                who = id2name.get(m.get("tool_call_id") or "", "")
                if who == "web_fetch":
                    n_fetch_err += 1
                elif who == "web_search" or not who:
                    # 认不出归属时算搜索错误 = 沿用旧行为,方向偏保守(宁可报警)
                    n_search_err += 1
                else:
                    n_other_err += 1
    names = Counter(tc["name"] for tc in tool_calls if tc.get("name"))
    return {
        "n_fetch_err": n_fetch_err, "n_other_tool_err": n_other_err,
        "final_answer": final_answer,
        "final_answer_len": len(final_answer) if final_answer else 0,
        "n_messages": len(trajectory), "n_assistant_turns": n_assistant,
        "n_tool_calls": len(tool_calls), "tools_used": dict(names),
        "browse_used": bool(BROWSE_TOOLS & set(names)),
        "n_search": n_search, "n_fetch": n_fetch,
        "n_empty_search": n_empty_search, "n_search_err": n_search_err,
        # ⚠️ 20260731 去掉 `[:50]`。它是**按臂相关的偏置源**,与题库泄露、serper 错误率
        # 同型:触顶题数按臂不同(T2c 语料轴 base 39 / dr17 58),所以任何从这两个字段
        # 算出来的率都被系统性压偏。实测差一个量级 —— 逐题逐字重复占比用截断字段算是
        # 1.1-1.8%,用未截断的 tool_calls 算是 8.7-11.9%,而且**语料轴上 dr 比 base 重复得少**,
        # 与 dr@1.6 的立项前提相反。凡用过这两个字段的 A−B 一律重算。
        # 同时落原始条数:以后要判"有没有需要截"不必再考古。
        "evidence_urls": evidence_urls, "search_queries": search_queries,
        "n_evidence_urls": len(evidence_urls), "n_search_queries": len(search_queries),
        "tool_calls": tool_calls,
    }


def done_keys() -> set[str]:
    return {f"{r['qid']}#s{r['sample_id']}"
            for r in read_jsonl(RAW)} if RAW.exists() else set()


async def run_one(seed: dict, sample_id: int, timeout: float,
                  sem: asyncio.Semaphore, env: dict, config: str,
                  msg_prefix: str = "", teacher_model: str = TEACHER_MODEL) -> dict:
    qid = seed["qid"]
    qtok = _tok(qid, 12)
    ws = WS_ROOT / f"{qtok}_s{sample_id}"
    ws.mkdir(parents=True, exist_ok=True)
    config = per_question_config(config, qtok, sample_id)
    env = web_ledger_env(env, qtok, sample_id)
    message = msg_prefix + seed["question"]
    cmd = [EVERCLAW, "agent", "-m", message, "-w", str(ws),
           "--no-markdown", "--no-logs", "--config", config]
    async with sem:
        t0 = time.monotonic()
        status, rc, out_b, err_b = "ok", None, b"", b""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env)

            # ── 活性看门狗 v2(2026-07-07):v1 用"无 session=挂死"是错误前提——
            # 实测 session 文件在任务**收尾**才落盘(文件名时间戳只是会话 ID 的铸造
            # 时刻),v1 会误杀一切 > watchdog 的正常长任务。v2 改用**滚动活性**:
            # raven 干活时 ws 下持续有文件活动(memory/.curator 每轮 LLM 交互落
            # trace、工具产物落盘);连续 watchdog 秒 ws 内**零文件活动**≈ 客户端
            # 可见的真死(LLM 调用永不返回/进程僵死)→ 击杀 status=launch_stall。
            # 注意:一次超长的单轮生成(推理马拉松)在本地同样零活动、无法与真死
            # 区分——所以 watchdog 也是"单轮生成时长上限",默认 600s,按教师速度调。
            # asyncio 细节:communicate() 只能有一个读者,用单一任务 + shield 轮询,
            # 超时窗口反复 wait 不取消它;击杀后 await 它回收缓冲输出。
            watchdog = float(os.environ.get("ROLLOUT_STARTUP_WATCHDOG", 600))
            t0_wall = time.time()
            comm = asyncio.ensure_future(proc.communicate())

            async def _kill_collect():
                proc.kill()
                try:
                    return await asyncio.wait_for(comm, timeout=15)
                except Exception:
                    comm.cancel()
                    return b"", b""

            while True:
                remaining = timeout - (time.monotonic() - t0)
                if remaining <= 0:
                    status = "timeout"
                    out_b, err_b = await _kill_collect()
                    break
                try:
                    out_b, err_b = await asyncio.wait_for(
                        asyncio.shield(comm), timeout=min(watchdog, remaining))
                    rc = proc.returncode
                    break
                except asyncio.TimeoutError:
                    try:
                        newest = max((f.stat().st_mtime
                                      for f in ws.rglob("*") if f.is_file()),
                                     default=0.0)
                    except OSError:
                        newest = 0.0
                    last_activity = max(newest, t0_wall)
                    if time.time() - last_activity > watchdog:
                        status = "launch_stall"   # 连续 watchdog 秒零活性
                        out_b, err_b = await _kill_collect()
                        break
                    # 有活动 → 滚动续等下一窗口
        except Exception as e:
            status, err_b = "launch_error", str(e).encode()
        elapsed = round(time.monotonic() - t0, 1)

    traj = read_session(ws)
    info = analyze(traj)
    if status == "ok" and not info["final_answer"]:
        status = "no_answer"
    harness_error = read_harness_error(ws)
    # ★★★ dr@2.3 (A2, 20260804):**答案可见性与死因升为一等字段。**
    #
    # 为什么需要它:块 1 五臂 600 行里 `status` 恒为 `ok` —— 包括 13 条打分后空答、
    # 3 条撞窗、3 条迭代满。那不是 bug:`status` 在**它自己的定义**下是字面真实的
    # (进程干净退出且产出了文本 —— 那 13 行确实产出了 10,669~58,237 字符)。
    # 真正的缺口是**没有任何字段说得出"答案为什么不可见"**:13 条里 10 条零死因记录。
    # ⇒ 所以 `status` **一字不动**(旧键,已发表数用过它;而且重定义它会同时武装
    #   `--reroll-failed` 的丢行与 `--finalizer` 的老师代答两条路径),新增下面三个字段。
    #
    # ⚠️⚠️ 判据必须与 scorer **同一个函数**,不是"同样的逻辑"。
    #   第一版我按"有闭标签且之后非空"自己写了一遍,在块 1 数据上回放得 dr22 22 条空答,
    #   而打分口径是 13 条 —— **差 9 条,因为 scorer 有 salvage 豁免**
    #   (`salvage_committed ⇒ exempt_closing_tag`:成功的 salvage 结构上必定不带闭标签,
    #   严格判据会把好答案清空;dr@1.6 为此付过 53 条)**外加 `<answer>` 标签提取**。
    #   ⇒ 自己重写就是造出第三把尺子,正是本次要消灭的东西。改为直接 import。
    #   同源于 `eval_clean_adapter` = "answer_rate 冻结定义" 的唯一实现。
    # ★★★ 20260804 事故修复(块 2 第一条臂在此 NameError 死掉、静默 1.5 小时)。
    # 两处改动,第二处是通则:
    #  ① `runtime_config` **在本函数里不存在** —— 它是 `main()` 的局部量,按位置传进来后
    #     形参叫 `config`(:444),而 :450 还把它重绑成**逐题**配置路径。
    #     `per_question_config` 只重写 `corpusEndpoint`、其余键原样拷贝 ⇒ 用 `config` 读
    #     `maxTokens`/`maxIterations` 不但正确,而且比读批级配置更准(它就是这题实际跑的那份)。
    #     ⚠️ 讽刺的是 `_caps` 的 docstring 写着"这个 helper 就是为了让 dict/路径 那个错不可能
    #     再犯" —— 它防住了类型,却没防住**作用域**。而 `ast.parse` 通过、3,891 条测试全绿,
    #     因为**没有任何测试进入 `run_one`**。⇒ 通则:一个只在跑批路径里才执行的分支,
    #     静态检查与单测都不构成它能跑的证据。
    #  ② **仪器不许杀死它所测量的批次。** 这一整块是 A2 仪器(分数预算 0),而它当时长在
    #     每题都会走的记录构造路径上 ⇒ 任何异常 = 整批死。所以下面兜一层,但**兜出来的值
    #     必须是可观测的**:`answer_visible=None`(不是 False —— False 会把有答案的题谎报成
    #     空答,直接污染 answer_rate 这个预注册二级终点;None 会让 `answer_rate_endpoint.py`
    #     的"落盘 vs 复算"交叉核对 rc=1 报错),`answerless_cause="diag_error"` 进枚举。
    #     ⇒ 仪器坏掉的后果从"批次死"变成"分析时一道红灯",而不是静默偏置。
    answer_visible = None
    finish_shape = "diag_error"
    answerless_cause = None
    answerless_budget = None
    answerless_diag = None
    try:
        _raw_ans = info["final_answer"] or ""
        _visible = _clean_extract(_raw_ans,
                                  exempt_closing_tag=_salvage_committed({"trajectory": traj}))
        answer_visible = bool((_visible or "").strip())
        _closed = "</think>" in _raw_ans.lower()
        finish_shape = ("closed_with_answer" if answer_visible else
                        "closed_but_silent" if _closed else
                        "unclosed_think" if _raw_ans.strip() else "empty_output")
        # 死因枚举。**顺序即优先级**,而且每一项都必须能从落盘证据独立复核。
        # `unknown` 保留但必须带诊断负载 —— 一个终点式的 "unknown" 下一批还是 unknown。
        _gen_cap_tok, _cap = _caps(config)
        if not answer_visible:
            _turns = info["n_assistant_turns"] or 0
            if harness_error == "context_overflow":
                answerless_cause = "context_overflow"
            elif status in ("timeout", "launch_stall", "launch_error"):
                answerless_cause = {"timeout": "watchdog_timeout",
                                    "launch_stall": "launch_stall",
                                    "launch_error": "launch_error"}[status]
            elif _turns >= _cap:
                answerless_cause = "iteration_cap"
            elif finish_shape == "unclosed_think":
                # 块 1 实测:这 7 条的 raw 长度 34,691~58,237 字符,而 maxTokens=16384
                # ⇒ 58,237/16,384 = 3.55 字符/token,正落在英文正常区间 ⇒ 撞单轮生成上限。
                # 分成两档报,因为修法不同:撞上限是**预防型**靶子,短的那档才是真"停下来了"。
                # 0.8 是余量:3.4 字符/token 是英文经验值(块 1 实测 3.55),取 80% 门槛避免
                # 把"刚好很长但没撞顶"误判成撞顶。误判方向要保守 —— 宁可少认一条预防型靶子。
                answerless_cause = ("gen_cap_midthink"
                                    if len(_raw_ans) >= 0.8 * 3.4 * _gen_cap_tok
                                    else "unclosed_think")
            elif finish_shape == "closed_but_silent":
                answerless_cause = "closed_but_silent"
            elif finish_shape == "empty_output":
                answerless_cause = "empty_output"
            else:
                answerless_cause = "unknown"
            # ★ 20260813 新增的**第二根轴**,与 `answerless_cause` 正交。
            # `answerless_cause` 一个字节都没改 —— 已发表批次的读者按旧定义读它,
            # 改它的取值会让"同一道题在新旧批次里死因不同"变成读数差(本项目在
            # `web.py` 的 `failed`/`k` 上已经立过这条规矩:保留旧键、新增新键)。
            #
            # 这根轴回答的是旧轴问不出来的那个问题:**它撞的是哪个预算**。
            #   gen_cap_nominal —— 撞的是配置里那个 ceiling ⇒ 加预算已实测无用
            #                      (`config/raven.py:1638`,4096→8192,p=0.23)
            #   gen_cap_shrunk  —— 撞的是 `_fit_request` 下调过的 reserve ⇒ 模型从未
            #                      拿到配置给它的预算,这是 reserve 策略的头寸,不是
            #                      "模型话太多"的头寸。**两组的臂相关方向相反**,
            #                      实测 web 轴:压缩组 dr 15 / 锚点 5,名义组 dr 14 / 锚点 41。
            #   not_budget      —— 这条死因与生成预算无关(撞窗、迭代上限、看门狗…)
            #   unknown         —— 没有 `turn_end` observer(它自己就是一条线索:
            #                      `bc-781` 那种 143 轮 overflow 死的题拿不到它)
            _te = _final_turn_end(traj)
            _eff_cap = _te.get("final_completion_cap")
            if not _te:
                answerless_budget = "unknown"
            elif _te.get("final_finish_reason") != "length":
                answerless_budget = "not_budget"
            elif _eff_cap is None:
                answerless_budget = "gen_cap_nominal"
            else:
                answerless_budget = "gen_cap_shrunk"
            answerless_diag = {"finish_shape": finish_shape, "raw_len": len(_raw_ans),
                               "turns": _turns, "iter_cap": _cap,
                               "gen_cap_tokens": _gen_cap_tok,
                               # 名义 cap 之外,最后那次调用**实际**拿到的预算。
                               # None ⇒ 名义值当时在生效(不是"取不到")。
                               "gen_cap_effective": _eff_cap,
                               "prefits": _te.get("prefits"),
                               "final_finish_reason": _te.get("final_finish_reason"),
                               "harness_error": harness_error, "status": status,
                               "visibility_source": _VIS_SRC,
                               "n_search": info["n_search"], "n_fetch": info["n_fetch"]}
    except Exception as _diag_exc:   # noqa: BLE001 —— 见上面 ② :仪器不许杀死批次
        answer_visible = None
        finish_shape = "diag_error"
        answerless_cause = "diag_error"
        # 与 `answerless_cause` 一起进 diag_error,不留 None —— None 在这根轴上
        # 的意思是"有答案,这一格不适用",拿它兼表"仪器坏了"就分不开两件事。
        answerless_budget = "diag_error"
        answerless_diag = {"error": f"{type(_diag_exc).__name__}: {_diag_exc}",
                           "visibility_source": _VIS_SRC}
    res = {
        "harness_error": harness_error,
        # dr@2.3 A2 新键(旧键一字不动)
        "answer_visible": answer_visible,
        "finish_shape": finish_shape,
        "answerless_cause": answerless_cause,
        # 20260813 新增:与 answerless_cause 正交的「撞的是哪个预算」轴。见上面的推导。
        # 顶层而不是只放 diag 里,因为要按它分组 —— 而 `rejected_on_elided` 那次的
        # 教训正是**嵌进一层就等于不存在**:字段一直在落盘,查顶层键的人判它缺失。
        "answerless_budget": answerless_budget,
        "answerless_diag": answerless_diag,
        "qid": qid, "sample_id": sample_id, "source": seed["source"],
        "lang": seed["lang"], "type": seed["type"], "model": teacher_model,
        "status": status, "returncode": rc, "elapsed_s": elapsed,
        "question": seed["question"], "gold_answer": seed.get("gold_answer", ""),
        "gold_kind": seed.get("gold_kind"), "hop": seed.get("hop"),
        # 打分口径随行透传:新目标套一批里混着 exact / table / report 三条打分通路,
        # 只有 exact 题有 gold_answer(table 的 gold 在 gold_table、report 无 gold)。
        # 不带这个字段,下游 06_validate 会把另外 300 题(21%)当成"空 gold"判错。
        "scorer": seed.get("scorer", "exact"),
        # W0.6(SFTv1 需求#7):种子标签(field/style/difficulty/target_chain…)
        # 随行透传到出货 meta——本轮 bench 标签全是常量,浪费了分析维度的教训。
        "labels": seed.get("labels") or {},
        "forced_search": bool(msg_prefix),
        **{k: info[k] for k in (
            "final_answer", "final_answer_len", "n_messages",
            "n_assistant_turns", "n_tool_calls", "tools_used", "browse_used",
            "n_search", "n_fetch", "n_empty_search", "n_search_err",
            # 20260806(Framework):新增键必须同时进这个**逐键白名单**,否则落盘层会
            # 静默丢掉它们 —— dr@1.7 首测就是这样整批惰性的(记忆
            # `dr16-salvage-seam-zeroes-itself`:改了代码、量的还是上一版行为)。
            "n_fetch_err", "n_other_tool_err",
            "evidence_urls", "search_queries")},
        "trajectory": traj,
        "tool_calls": info["tool_calls"], "workspace": str(ws),
        "stderr_tail": err_b.decode("utf-8", "replace")[-1500:],
    }
    flag = "✓" if status == "ok" else status
    if harness_error:
        flag = f"!{harness_error}"
    print(f"[{qid:>16} s{sample_id}] {flag:>17} {elapsed:>6}s  "
          f"search={info['n_search']} fetch={info['n_fetch']} "
          f"empty={info['n_empty_search']} ans_len={info['final_answer_len']}",
          flush=True)
    return res


def _write_rollout_pid() -> Path | None:
    """把自己的 PID 落进 run 目录,让清理能用「自己的 PID」而不是共享名字。

    用户明令#7 的落地件。20260805 那次互杀事故的根因是分工按**可写路径**划,而进程
    不属于任何路径 —— 共享代码必然共享命令行,所以 `pkill -f 04_rollout` 天然跨 session。
    `safe_rollout_pids.sh --kill-mine <pidfile>` 早就写好了,缺的就是这个文件本身:
    没有它,想清理的人手上只剩 `-f`。

    ⚠️ 写失败不能停批:它是清理用的便利件,不是正确性件。一个能杀掉跑批的"安全设施"
    是净负。
    """
    try:
        pf = DATA / "rollout.pid"
        pf.write_text(f"{os.getpid()}\n")
        return pf
    except Exception as exc:            # noqa: BLE001
        print(f"[warn] 落 rollout.pid 失败({exc}) —— 清理只能靠人工核 /proc/<pid>/cmdline")
        return None


async def main() -> None:
    _write_rollout_pid()
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--samples-by-source", type=str, default=None,
                    help="per-source sample override, e.g. "
                         "'quest=3,infoseek=4,openseeker=8' (sample HARD/low-p "
                         "sources more); sources not listed fall back to --samples")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--only", type=str, default=None)
    ap.add_argument("--config", type=str, default=str(DEFAULT_CONFIG),
                    help="template config (no secrets); key injected at runtime")
    ap.add_argument("--force-search", action="store_true",
                    help="prepend the search-forcing instruction (§6.5 tool-collapse fix)")
    ap.add_argument("--anti-overthink", action="store_true",
                    help="stronger prefix: search-forcing + cap rumination (zh hang fix)")
    ap.add_argument("--fetch-forcing", action="store_true",
                    help="病根验证: 纯 fetch-forcing 前缀(每次 search 后强制 fetch 正文, "
                         "隔离检索-消化链坍缩单变量)")
    ap.add_argument("--persist-concede", action="store_true",
                    help="W2 认输课前缀: 坚持~20次搜索, 预算尽给最佳候选+明示不确定 "
                         "(配 GIVEUP_MODE=1 判官三查)")
    ap.add_argument("--persistence", action="store_true",
                    help="W3 链长课程前缀: 反早收敛, 每约束单独检索+fetch 核实 "
                         "(12-20+ 次搜索)")
    ap.add_argument("--nudge-prob", type=float, default=1.0,
                    help="probability a run gets the forcing prefix (deterministic "
                         "per qid#sample). <1.0 decorrelates search behavior from the "
                         "prefix (PROTO-06); 放量建议 0.6~0.8")
    ap.add_argument("--drop-recitable", action="store_true",
                    help="skip seeds the recite gate (★b) flagged recite_hit=True "
                         "(student can already answer them closed-book, §2)")
    ap.add_argument("--finalizer", action="store_true",
                    help="salvage no_answer runs that gathered evidence: one tool-less "
                         "teacher call over the evidence, appended as the terminal answer "
                         "turn (治 Kimi finalization 不稳)")
    ap.add_argument("--reroll-failed", action="store_true",
                    help="drop failed rows (timeout/no_answer/launch_error) from traj_raw "
                         "before starting (backed up), so ONLY the failed qids re-roll while "
                         "the ok rows stay banked. Use with a lower --concurrency.")
    ap.add_argument("--print-ws-root", action="store_true",
                    help="打印本臂的工作区根后退出。起批门用它核**真实**路径里有没有"
                         "可读标识 —— 门自己复算一遍哈希只能证明门和代码用了同一个"
                         "算法,证明不了落到 prompt 里的那串字是干净的。")
    args = ap.parse_args()
    if args.print_ws_root:
        print(WS_ROOT)
        return
    prefix_mode = ("persist_concede" if args.persist_concede
                   else "persistence" if args.persistence
                   else "anti_overthink" if args.anti_overthink
                   else "fetch_forcing" if args.fetch_forcing
                   else "force_search" if args.force_search else "")

    # render key-injected config to system temp (keeps secret out of this folder)
    runtime_config = render_runtime_config(Path(args.config))
    # 记账用真实教师模型:从所选 config 读 agents.defaults.model(model 非机密),
    # 而非硬编码常量——换 teacher(如 Volc 网关 Kimi-K2.7-code)时 provenance 才准确。
    try:
        _cfg = json.loads(Path(args.config).read_text())
        _defaults = _cfg.get("agents", {}).get("defaults", {})
        teacher_model = _defaults.get("model") or TEACHER_MODEL
        teacher_provider = (_defaults.get("provider") or "").lower()
    except Exception:
        teacher_model, teacher_provider = TEACHER_MODEL, ""

    seeds = read_jsonl(SEEDS) or read_jsonl(SEEDS_FALLBACK)
    if args.only:
        want = {x for x in args.only.split(",") if x}
        seeds = [s for s in seeds if s["qid"] in want]
    if args.drop_recitable:
        n0 = len(seeds)
        seeds = [s for s in seeds if s.get("recite_hit") is not True]
        print(f"--drop-recitable: dropped {n0 - len(seeds)} recitable seed(s)")
    # 工作区搬出 rundir 之后,批次脚本那句 `rm -rf $rundir` 就不再顺手清掉它了。
    # 不清是有后果的:上一批留下的 user_memory/episodic 会被这一批的同题读到 ——
    # 这正是官方臂踩过的"memory 跨题污染"那个坑,只是换成了跨批次。判据用
    # traj_raw 是否存在(= 这条臂是不是全新开跑),`--only` 的定点补跑不清。
    if not RAW.exists() and not args.only and WS_ROOT.exists():
        shutil.rmtree(WS_ROOT, ignore_errors=True)
        print(f"[ws] 清理上一批残留工作区:{WS_ROOT}")
    WS_ROOT.mkdir(parents=True, exist_ok=True)
    (DATA / "workspaces_root.txt").write_text(str(WS_ROOT) + "\n")
    write_jsonl(WS_MAP, [{"qid": s["qid"], "ws_token": _tok(s["qid"], 12),
                          "arm_token": _ARM_TOK, "batch_token": _BATCH_TOK}
                         for s in seeds])
    env = rollout_env()
    # SERPER 恒需(web_search);教师密钥按 provider 判断:deepseek 直连需 DEEPSEEK_API_KEY,
    # openrouter 需 OPENROUTER_API_KEY,custom(Volc 网关 Kimi)网关免密不检查。
    if "SERPER_API_KEY" not in env:
        print("WARN: missing SERPER_API_KEY in .env (web_search will fail)")
    if teacher_provider == "deepseek" and "DEEPSEEK_API_KEY" not in env:
        print("WARN: teacher provider=deepseek 但缺 DEEPSEEK_API_KEY")
    if teacher_provider == "openrouter" and "OPENROUTER_API_KEY" not in env:
        print("WARN: teacher provider=openrouter 但缺 OPENROUTER_API_KEY")

    by_source: dict[str, int] = {}
    if args.samples_by_source:
        for part in args.samples_by_source.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                by_source[k.strip()] = int(v)

    def n_samples(seed: dict) -> int:
        return by_source.get(seed["source"], args.samples)

    # --reroll-failed: drop failed rows from traj_raw (backed up) so their qids
    # re-roll while ok rows stay banked (done_keys skips them). Use w/ lower conc.
    if args.reroll_failed and RAW.exists():
        _FAIL = {"timeout", "no_answer", "launch_error", "error", "launch_stall"}
        all_rows = read_jsonl(RAW)
        keep = [r for r in all_rows if r.get("status") not in _FAIL]
        dropped = len(all_rows) - len(keep)
        if dropped:
            bak = RAW.with_suffix(".jsonl.prereroll.bak")
            write_jsonl(bak, all_rows)
            write_jsonl(RAW, keep)
            from collections import Counter as _C
            drop_by = _C(r["status"] for r in all_rows if r.get("status") in _FAIL)
            print(f"--reroll-failed: dropped {dropped} failed rows {dict(drop_by)} "
                  f"(backup → {bak.name}); {len(keep)} ok rows kept, their qids will re-roll")

    done = done_keys()
    jobs = [(s, sid) for s in seeds for sid in range(n_samples(s))
            if f"{s['qid']}#s{sid}" not in done]
    if args.limit is not None:
        jobs = jobs[:args.limit]
    samples_desc = (", ".join(f"{k}={v}" for k, v in by_source.items()) +
                    f", default={args.samples}") if by_source else str(args.samples)
    print(f"rollout: {len(jobs)} runs (samples={samples_desc}, "
          f"concurrency={args.concurrency}, timeout={args.timeout}s, "
          f"model={teacher_model}); {len(done)} already done")

    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()

    # ── 熔断器(2026-07-07 prod_glm300 教训):尾程端点死亡(connection refused /
    # finish_reason=abort)后,队列剩余 80 条 openseeker 在 20 分钟内被灌进死端点
    # 全灭。连续 ROLLOUT_BREAKER_N 条"零轮失败"→ 1-token 探活:活=瞬时抖动清零
    # 继续;死=置 abort,余下任务不再发射(不写 RAW → done_keys 缺位,端点恢复后
    # 直接重跑/--reroll-failed 即可续滚)。
    breaker_n = int(os.environ.get("ROLLOUT_BREAKER_N", 6))
    consec_bad = 0
    n_skipped = 0
    abort_evt = asyncio.Event()

    def _probe_endpoint() -> bool:
        """1-token 探活(blocking,经 to_thread 调)。读 runtime config 里当前
        provider 的 apiBase/apiKey;非 custom 网关无 apiBase 可探 → 视为活,
        绝不误熔断。旁路代理(volceapi 内网直达)。"""
        try:
            cfg = json.loads(Path(runtime_config).read_text())
            defaults = (cfg.get("agents", {}) or {}).get("defaults", {}) or {}
            prov = (cfg.get("providers", {}) or {}).get(
                defaults.get("provider", ""), {}) or {}
            base = prov.get("apiBase")
            if not base:
                return True
            body = json.dumps({
                "model": defaults.get("model", ""), "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}]}).encode()
            req = urllib.request.Request(
                f"{base.rstrip('/')}/chat/completions", data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {prov.get('apiKey') or 'EMPTY'}"})
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=45):
                return True
        except Exception as e:
            print(f"[breaker] endpoint probe FAILED: {str(e)[:200]}", flush=True)
            return False

    async def worker(seed, sid):
        nonlocal consec_bad, n_skipped
        if abort_evt.is_set():
            n_skipped += 1
            return None
        res = await run_one(seed, sid, args.timeout, sem, env, runtime_config,
                            msg_prefix=pick_prefix(prefix_mode, seed, sid,
                                                   args.nudge_prob),
                            teacher_model=teacher_model)
        # no-tools finalizer: salvage a no_answer run that DID gather evidence
        if (args.finalizer and res.get("status") == "no_answer"
                and (res.get("n_search") or res.get("n_fetch"))):
            async with sem:                      # respect the concurrency cap for the extra call
                ans = await finalize(seed, res.get("trajectory") or [], args.config)
            if ans:
                traj = res.get("trajectory") or []
                traj.append({"role": "assistant", "content": ans})
                res["trajectory"] = traj
                res["final_answer"] = ans
                res["final_answer_len"] = len(ans)
                res["status"] = "ok"
                res["finalized"] = True
                print(f"[{seed['qid']:>16} s{sid}]   finalized  (+{len(ans)} chars answer)", flush=True)
        # 熔断记账:零轮失败(端点级死亡的指纹)连击 → 探活 → 死则 abort。
        zero_turn_fail = (res["status"] in ("launch_stall", "launch_error")
                          or (res["status"] in ("no_answer", "timeout")
                              and not res.get("n_assistant_turns")))
        if zero_turn_fail:
            consec_bad += 1
            if consec_bad >= breaker_n and not abort_evt.is_set():
                print(f"[breaker] {consec_bad} 连零轮失败 → 探活端点…", flush=True)
                if await asyncio.to_thread(_probe_endpoint):
                    print("[breaker] 端点存活 → 视为瞬时抖动,计数清零", flush=True)
                    consec_bad = 0
                else:
                    abort_evt.set()
                    print("🔴 [breaker] 端点死亡 → 停止发射剩余任务(已完成的保留;"
                          "端点恢复后重跑本命令即自动续滚缺位 qid)", flush=True)
        else:
            consec_bad = 0
        async with lock:
            with RAW.open("a") as f:
                f.write(json.dumps(res, ensure_ascii=False) + "\n")
        return res

    await asyncio.gather(*(worker(s, sid) for s, sid in jobs))
    if abort_evt.is_set():
        print(f"\n🔴 [breaker] 本轮因端点死亡提前熔断:{n_skipped} 个任务未发射"
              f"(未写 RAW,端点恢复后重跑即续滚)", flush=True)

    # search-health + status summary over everything on disk
    allr = read_jsonl(RAW)
    st = Counter(r["status"] for r in allr)
    tot_s = sum(r.get("n_search", 0) for r in allr)
    tot_empty = sum(r.get("n_empty_search", 0) for r in allr)
    tot_err = sum(r.get("n_search_err", 0) for r in allr)
    tot_f = sum(r.get("n_fetch", 0) for r in allr)
    tot_ferr = sum(r.get("n_fetch_err", 0) for r in allr)
    health = {
        "n_runs": len(allr), "status": dict(st),
        "browse_used": sum(1 for r in allr if r.get("browse_used")),
        "total_searches": tot_s, "empty_search_rate":
            round(tot_empty / tot_s, 3) if tot_s else None,
        "search_error_rate": round(tot_err / tot_s, 3) if tot_s else None,
        # 20260806(Framework):**fetch 面必须有自己的率**,分母是 fetch 次数。
        # dr@2.4 web 轴实测的教训:那批 9,187 次 fetch **全部失败**(r.jina.ai 402
        # 欠费),而唯一亮红灯的是 `search_error_rate` —— 它之所以亮,只是因为它错把
        # fetch 的错误算进了分子。**把分子改对而不补这道门,下一批就会亮着绿灯放行
        # 一个读不了任何网页的 harness。**(门必须量它名字说的东西,且不能因此少量。)
        "total_fetches": tot_f,
        "fetch_error_rate": round(tot_ferr / tot_f, 3) if tot_f else None,
        "total_other_tool_err": sum(r.get("n_other_tool_err", 0) for r in allr),
        "breaker_abort": abort_evt.is_set(),
        "n_skipped_by_breaker": n_skipped,
    }
    health.update(_ledger_health())
    (DATA / "rollout_health.json").write_text(
        json.dumps(health, ensure_ascii=False, indent=2))
    print("\n=== rollout health ===")
    print(json.dumps(health, ensure_ascii=False, indent=2))
    # ★ 20260813 Framework 修「门与它的显示不同源」。原写法是 `if tot_err:` ——
    # 判据用**原始计数**,而同一块 JSON 里印的是 `search_error_rate`(保留三位小数)。
    # 实测消融批 eval_corpus_abl_20260813:2 次错 / 15,299 次检索 ⇒ 同一屏上同时出现
    # `"search_error_rate": 0.0` 和 `🔴 search errors detected`。
    # 读者只能二选一相信,而两个都是真的 —— 那不是矛盾,是**两个不同的量被当成一个**。
    # ⇒ 印**原始计数**(率不是判据),并把率一起带上,让"为什么红"从输出里直接可读。
    # 阈值仍是 1:检索错误是污染信号,不是性能指标,一次也要能看见。**不改判据的严格度**,
    # 只让显示与判据同源 —— 把它改成"按率判"会在 15,299 次里放过 15 次错。
    if tot_err:
        rate = round(tot_err / tot_s, 3) if tot_s else None
        print(f"🔴 search errors detected: {tot_err} of {tot_s} searches "
              f"(search_error_rate={rate} — rounded, NOT the criterion; "
              f"the criterion is the raw count >= 1) "
              f"— possible silent contamination (§1.3)")


if __name__ == "__main__":
    asyncio.run(main())
