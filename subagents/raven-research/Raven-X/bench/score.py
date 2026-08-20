#!/usr/bin/env python3
#
# ═══════════════════════════════════════════════════════════════════════════
#  判分引擎(exact / table / report 三个判官在此分流)
#
#  20260811 Framework:内容从 `pipeline/06_validate.py` 搬到这里 —— 用户要求「RavenX 相关
#  的内容文件都在同一个文件夹」。**这是唯一真相源**;`pipeline/06_validate.py` 已改成
#  一层薄转发器(`runpy.run_path`),不是第二份实现。
#
#  ⚠️ 为什么不能是「复制」:runner 执行的入口是 `pipeline/06_validate.py`,若两处各留一份
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
"""Stage ④ Validation → data/traj_scored.jsonl

Two axes (§5.1):
  - DeepSearch (DS): verifiable gold. Try normalized exact/substring match;
    if no match, fall back to a DeepSeek LLM-judge equivalence check.
    Also computes per-hop evidence coverage here (建议#5, moved upstream from ⑥
    so the filter ⑤ can gate on it — see 报告生成优化方案 §4).
  - DeepResearch (DR): no single answer → 4-dim LLM-as-judge
    (comprehensiveness / insight / instruction-following / readability, 0–10),
    PLUS a 5th axis — online fact-check (建议#3 / 优化方案 §3): pull 1–2 checkable
    claims from the report, verify each via live Serper search. A REFUTED claim
    VETOES the report regardless of how well it is written (fixes the observed
    "文笔 8.11 / 事实 0.20 仍被选中" failure). pass = avg4 >= 6.0 AND not refuted.
Also records evidence_hit (did the answer rest on a real web_fetch'd page, RQ5).
Judge = Claude Sonnet via OpenRouter by default (cross-family — avoids the
same-family self-preference bias of grading deepseek-v4-pro with a deepseek
judge; see common.judge_chat). Set JUDGE_BACKEND=deepseek to fall back to the
cheaper same-family deepseek-v4-flash. DS's match_ds string path is unaffected.

Usage: python 06_validate.py [--no-factcheck] [--factcheck-claims 2] [--golden]
       JUDGE_BACKEND=sonnet|deepseek (default sonnet)
"""
from __future__ import annotations

import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import (DATA, evidence_text, hop_coverage, judge_chat, judge_identity,
                    load_env,
                    norm, parse_json_loose, read_jsonl, serper_search,
                    write_jsonl)

# 损坏 gold(教师轨迹碎片混入 gold 字段):QUEST 池的 <tool_call>/<think> 残片。
# PROTO-02 实锤:judge_dr 曾把这种垃圾当参照消费(quest-61 在 262 字符 <tool_call>
# 参照下判 correct 发货)→ 判分前拦截,裁判降级为无参照打分。
CORRUPT_GOLD_RE = re.compile(r"^\s*<(tool_call|think|tool_response)\b", re.I)


def gold_is_corrupt(gold: str) -> bool:
    return bool(gold) and bool(CORRUPT_GOLD_RE.match(gold))


# W0.3 认输通道(SFTv1 需求#1,最高优先):训练集 99.94% 成功偏置(correct=True
# 7155/7159,认输语言 0.15%)= 模型从未见过"搜索失败→体面收尾",预算耗尽即落入
# 训练分布外→think 死循环。GIVEUP_MODE=1(认输课批次 W2 专用旗标,常规批默认关)
# 时,DR 判 incorrect 的行追加"三查";全过 → verdict=honest_giveup,07 走专用
# 保留通道以 correct=False 出货。gold 不参与毙权(认输行本来就没答对,判的是
# 失败处理质量);gold 只留作"真难度"统计。
GIVEUP_MODE = os.environ.get("GIVEUP_MODE", "0").strip().lower() in ("1", "true", "yes")
# pilot v1 校准(20260715):难题池实际 n_search 中位=6(仍高于训练 p50=3),
# 8/2 首版从严会毙掉主体 → 5/2(仍要求真读页,守"只搜不读"反面示范红线)。
GIVEUP_MIN_SEARCH = int(os.environ.get("GIVEUP_MIN_SEARCH", "5"))
GIVEUP_MIN_FETCH = int(os.environ.get("GIVEUP_MIN_FETCH", "2"))

# 认输语义须落在结尾(与 07 _honest_failure 的 hedge 口径同理):en+zh 双语。
_HEDGE_TAIL_RE = re.compile(
    r"uncertain|cannot\s+(be\s+)?(confirm|verif|determin|ruled?\s+out|rank)|"
    r"could\s*n[o']?t\s+(confirm|verify|determine|find)|"
    r"not\s+(fully\s+)?(certain|confident|conclusive)|"
    r"unable\s+to\s+(confirm|verify|determine)|inconclusive|"
    r"insufficient\s+(evidence|information|data)|"
    r"best[- ](supported[- ])?(candidate|guess)|most\s+likely|"
    r"无法(确认|证实|确定|找到|排除)|不(能|敢)?确定|未能(找到|确认|证实)|不确定|存疑|"
    r"最(有依据|可能)的?候选|证据不足|信息不足", re.I)


def giveup_check(r: dict, ans: str, ev: str, judge_fn=judge_chat) -> dict:
    """认输三查:①检索尽职(客观计数——"没怎么搜就投降"不配当示范);②候选有据
    ③不确定明示(LLM 判官)+ 结尾 hedge 正则双保险(都过才 pass,首版从严,
    W2 pilot 50 人读后再校准)。judge_fn 可注入(golden 用桩)。"""
    ns, nf = r.get("n_search") or 0, r.get("n_fetch") or 0
    out = {"pass": False, "n_search": ns, "n_fetch": nf,
           "diligent": ns >= GIVEUP_MIN_SEARCH and nf >= GIVEUP_MIN_FETCH,
           "hedge_tail": bool(_HEDGE_TAIL_RE.search((ans or "")[-1500:]))}
    if not out["diligent"]:
        out["why"] = (f"not diligent: search {ns}<{GIVEUP_MIN_SEARCH} "
                      f"or fetch {nf}<{GIVEUP_MIN_FETCH}")
        return out
    j = judge_fn([
        {"role": "system", "content":
         "You audit a research agent's FAILURE HANDLING. The answer is already "
         "known to not match the reference — that is expected and NOT what you "
         "judge. Judge three things:\n"
         "1. candidate_grounded — the answer commits to ONE specific best-guess "
         "candidate (or explicitly says every candidate was ruled out). The "
         "candidate need NOT be confirmed by the evidence — this is a give-up "
         "answer, so an unverified best guess is EXACTLY right. Mark true unless "
         "the answer FABRICATES sources/citations or asserts false confidence "
         "(claims something is verified when the evidence does not show it), or "
         "gives no concrete candidate at all.\n"
         "2. uncertainty_explicit — near its END the answer clearly acknowledges "
         "the conclusion is uncertain / the evidence was insufficient, ideally "
         "noting what was ruled out.\n"
         "3. no_loop_or_filler — coherent prose, no repetition padding.\n"
         'Reply ONLY JSON: {"candidate_grounded":true|false,'
         '"uncertainty_explicit":true|false,"no_loop_or_filler":true|false,'
         '"why":"..."}'},
        {"role": "user", "content": f"Task: {r.get('question', '')}\n\n"
                                    f"Final answer:\n{(ans or '')[:8000]}\n\n"
                                    f"Search evidence digest:\n{(ev or '')[:10000]}"},
    ], max_tokens=1024)
    d = parse_json_loose(j, {})
    for k in ("candidate_grounded", "uncertainty_explicit", "no_loop_or_filler"):
        out[k] = bool(d.get(k))
    out["judge_why"] = str(d.get("why") or "")[:300]
    out["pass"] = (out["diligent"] and out["hedge_tail"]
                   and out["candidate_grounded"] and out["uncertainty_explicit"]
                   and out["no_loop_or_filler"])
    return out


# 判官布尔与自述理由自相矛盾(审计 B3 实锤:openseeker-93 的 why 写着 "does not
# match the gold answer" 却给 correct=true,错答以正样本发货=训练投毒)。命中时
# 判决不可信 → 打 judge_failed(保守桶:既不进 correct 也不进 RQ1 诚实失败)。
_JUDGE_CONTRA_RE = re.compile(
    r"does\s*not\s+match|doesn'?t\s+match|do\s+not\s+match|"
    r"not\s+(the\s+)?(same|equivalent|a\s+match)|mismatch|"
    r"不匹配|不一致|不相符|不等同|与.{0,12}不符", re.I)


def match_ds(gold: str, ans: str) -> bool:
    g, a = norm(gold), norm(ans)
    if not g:
        return False
    # 否定式守卫(审计第一档#2):答案把 gold 作为被否定对象提及("not X"/"不是 X")
    # 时,g in a / token-recall 都会假阳 → 直接判不匹配,交 LLM 判官走语义路。
    if re.search(rf"\b(not|no|isn'?t|wasn'?t|不是|并非|而非)\s+(the\s+)?{re.escape(g)}", a):
        return False
    if g in a:
        return True
    # token-recall: most gold tokens present (handles "X congolensis" in prose)
    gt = g.split()
    return bool(gt) and sum(t in a for t in gt) / len(gt) >= 0.9


_SALVAGE_CORRECT_RE = re.compile(r'"correct"\s*:\s*(true|false)', re.I)
_SALVAGE_BOXED_RE = re.compile(r"\\boxed\{+\s*\"?(in)?correct\b", re.I)
_SALVAGE_WHY_RE = re.compile(r'"why"\s*:\s*"([^"]*)', re.S)


def _salvage_verdict(text: str, default: dict) -> dict | None:
    """截断残文的末路抢救:限 correct/why 判官形态。两种实测形态(20260724):
    ①输出在闭括号前被切断但 "correct" 布尔可见 → 取最后一个(与 _parse_json 取
    最后平衡对象同序);②判官不守格式回 \\boxed{correct}/\\boxed{incorrect}。
    命中打 judge_salvaged 供审计;评分维度型判官(judge_dr,default 无 correct
    键)不抢救,照旧 judge_failed。"""
    if "correct" not in default or not text:
        return None
    verdict = None
    hits = _SALVAGE_CORRECT_RE.findall(text)
    if hits:
        verdict = hits[-1].lower() == "true"
    else:
        boxed = _SALVAGE_BOXED_RE.search(text)
        if boxed:
            verdict = not boxed.group(1)
    if verdict is None:
        return None
    why = _SALVAGE_WHY_RE.findall(text)
    return {"correct": verdict,
            "why": (why[-1][:300] if why else "salvaged from malformed judge output"),
            "judge_salvaged": True}


def _judged(messages: list, default: dict, max_tokens: int = 4096,
            retries: int = 1) -> dict:
    """judge_chat + JSON 解析,失败重试且 max_tokens 逐次翻倍 —— parse_fail 的
    主形态是输出在收尾 JSON 闭括号前被 max_tokens 切断(20260724 八源 A/B 实锤:
    6 条 raw 内 verdict 可见却按默认 incorrect 发货,系统性偏压长答案臂),同参
    重试对确定性截断无效。仍失败先做保守正则抢救(judge_salvaged 打标),最后
    才 judge_failed —— CORR-03 老约定:双失败打标而非静默用默认值。

    ★ 20260813 Framework 改:**判官身份在这里盖,不在调用方盖**。
    原先只有 `score_one` 的 DS-LLM 分支记得写 `j["identity"] = judge_identity()`,
    而它旁边的注释写的正是「靠调用方记得设不是可自证的台账 —— MiroFlow 那批就是
    这么变成判官不可考的」。**然后下一个写的判官调用方就忘了**:
    `pipeline/rescore_short_gold.py:162` 的 `match_rescored_llm` 走同一个 `judge_ds`
    却不补这一行,实测 eval_web_dr30_20260812 四条臂各 132/153/140/156 行
    `judge.identity` 全为 null —— 而那是**重判过的**行,即判决真由判官做出的那些。
    同一批的 `judge_dr` 分支同样从未盖过。

    ⇒ 结构性修法不是在第二个调用点补第二行(那只是给第三个调用者重新上膛),
    是把它盖进判官调用本身。这里是 `judge_ds` / `judge_dr` 唯一的公共咽喉。
    纯字符串 match 分支仍显式写 `identity: None`,因为那里**确实没有判官**,
    "没有身份"和"忘了记身份"必须是两行不同的记录。
    """
    d, out = default, ""
    mt = max_tokens
    result = None
    for _ in range(retries + 1):
        out = judge_chat(messages, max_tokens=mt)
        d = _parse_json(out, default)
        if "raw" not in d:
            result = d
            break
        mt *= 2
    if result is None:
        salvaged = _salvage_verdict(out, default)
        if salvaged is not None:
            result = salvaged
        else:
            d["judge_failed"] = True
            result = d
    # 每条返回路径都盖,包括 judge_failed 和 salvaged ——
    # 判失败的那些行同样需要知道是谁判失败的。
    result["identity"] = judge_identity()
    return result


def judge_ds(question: str, gold: str, ans: str) -> dict:
    # 04 的 DS 终答上限 4000 字符,全量入判(旧 [:2000] 会截掉长推导答案的结论段)
    return _judged([
        {"role": "system", "content": "You are a strict grader. Decide if the "
         "candidate answer is equivalent to the gold answer for the question. "
         'End your reply with ONLY compact JSON: {"correct": true|false, "why": "..."}.'},
        {"role": "user", "content": f"Question: {question}\nGold: {gold}\n"
         f"Candidate: {ans[:4000]}"},
    ], {"correct": False, "why": "judge_parse_fail"})


# 人读审计 §7.2.3 盲区②:rubric judge 用过时参数知识否决轨迹内多源一致证据
# ("Rush 不可能巡演"/"EARTH 不是 KU"),与自家 factcheck(2/2 SUPPORTED)直接
# 冲突而 verdict 仍取 incorrect。检测判词中的"事实不信"语式,命中且 factcheck
# 证据侧支持时 → 用证据加持的提示重判一次(仲裁)。
_DISBELIEF_RE = re.compile(
    r"fabricat|hallucinat|invented|made.?up|does not exist|no such|"
    r"not (a |an )?(real|actual)|cannot (be|have)|couldn'?t have|impossible|"
    r"never (happened|toured|won|existed|took place)|"
    r"did not (happen|tour|win|exist|take place)|(is|are|was|were) not led by|"
    r"appears? (to be )?(fake|fictional|fabricated)|outdated|"
    r"(as of|based on) my knowledge|(不存在|并非真实|不可能|编造|捏造|虚构)", re.I)

_EVIDENCE_NOTE = (
    " IMPORTANT: An independent LIVE web fact-check has verified this report's "
    "key claims as SUPPORTED by current sources. Your training knowledge may be "
    "outdated relative to the report's subject. Do NOT down-score claims merely "
    "because they conflict with your prior knowledge; grade comprehensiveness, "
    "insight, instruction-following and readability of the report as given.")


def judge_dr(question: str, ans: str, gold_ref: str, evidence_note: str = "") -> dict:
    ref_line = (f"Reference (teacher, for grounding only): {gold_ref[:1500]}\n\n"
                if gold_ref else "")
    # 盲区①修复:废 [:4000] 前缀评分(22.5KB 完整报告曾被判 "severely truncated"
    # =假负)。04 的 DR 上限 30000 字符,预算内全量入判。
    d = _judged([
        {"role": "system", "content": "You grade a deep-research report on 4 "
         "dimensions, each 0-10: comprehensiveness, insight, instruction_following, "
         'readability. Reply ONLY compact JSON like '
         '{"comprehensiveness":8,"insight":7,"instruction_following":9,'
         '"readability":8,"why":"..."}.' + evidence_note},
        {"role": "user", "content": f"Task: {question}\n\n{ref_line}"
         f"Report to grade:\n{ans[:30000]}"},
    ], {})
    dims = ["comprehensiveness", "insight", "instruction_following", "readability"]
    if not any(k in d for k in dims):       # 解析出了 JSON 但不是评分对象 → 同样算失败
        d["judge_failed"] = True
    vals = [float(d.get(k, 0) or 0) for k in dims]
    d["avg"] = round(sum(vals) / len(dims), 2) if vals else 0.0
    return d


def _jina_fetch(url: str, max_chars: int = 6000) -> str:
    """Fetch page text via Jina Reader(升级 factcheck 证据:snippet-only → 页面级)。
    CORR-02/RQ1-02 实锤:quest-20 的数字逐字在官方 press release 页面上,却被只看
    snippet 的 factcheck + judge 过期知识联手误杀。失败返回 ''(降级回 snippet)。"""
    import urllib.request
    key = load_env().get("JINA_API_KEY", "")
    req = urllib.request.Request(
        f"https://r.jina.ai/{url}",
        headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return resp.read().decode("utf-8", "replace")[:max_chars]
    except Exception:
        return ""


def factcheck_dr(question: str, report: str, n_claims: int = 2,
                 fetch_pages: int = 1) -> dict:
    """③ Online fact-check: extract the N most checkable atomic claims from the
    report, verify each against live web search. verdict='refuted'(=veto)按 W0.1
    新口径:≥2 claim 被驳,或唯一被驳 claim 承重(见 fc_aggregate);单驳非承重 →
    'mixed_minor_refuted'(不毙);'supported'/'unverifiable'/'skipped' else.
    fetch_pages>0: 额外抓取 top-N 结果的正文喂给裁判(页面级证据,防 snippet 误杀)。"""
    # 1) pull the most concrete, verifiable claims (names/dates/numbers/events)
    # 盲区①修复:废 [:6000] 前缀抽取(编造集中在报告中后段=旧死角);并要求 claim
    # 分散取自不同章节、优先量化表格/区间(人读证实这是编造最毒的形态)。
    ex = judge_chat([
        {"role": "system", "content": "Extract the most CHECKABLE atomic factual "
         "claims from the report — prefer specific names, dates, numbers, places, "
         "or events that a web search could confirm or refute. Ignore opinions and "
         "generalities. Choose claims spread across DIFFERENT sections of the "
         "report — include its middle and final sections, not only the opening. "
         "Prioritize quantitative tables, price/time ranges and percentages if "
         f'present. Reply ONLY JSON: {{"claims":["...", "..."]}} (at most {n_claims}).'},
        {"role": "user", "content": f"Task: {question}\n\nReport:\n{report[:30000]}"},
    ], max_tokens=2048)
    claims = [c for c in (parse_json_loose(ex, {}).get("claims") or []) if c][:n_claims]
    if not claims:
        return {"verdict": "skipped", "note": "no checkable claim extracted",
                "checked": 0, "results": []}

    results = []
    for claim in claims:
        hits = serper_search(claim, n=5)
        if not hits:                       # no search backend / no results
            results.append({"claim": claim, "verdict": "unverifiable",
                            "note": "no search results"})
            continue
        ev = "\n".join(f"- {h['title']}: {h['snippet']}" for h in hits if h.get("snippet"))
        for h in hits[:max(0, fetch_pages)]:      # 页面级证据(可选,默认 top-1)
            if h.get("link"):
                page = _jina_fetch(h["link"])
                if page:
                    ev += f"\n--- page: {h['link']} ---\n{page}"
        # W0.1(判官审计 20260714):旧 prompt 下"证据没确认"被误标 REFUTED,高
        # rubric 答案误驳率 57%(NIST SP1270/CFPP $9.3B 等真 claim 全中招)——
        # REFUTED 必须是证据正面矛盾;沉默/擦边/查无 = UNVERIFIABLE;禁凭记忆定罪
        # (quest-20:judge 过时先验压过页面逐字证据的老坑)。
        j = judge_chat([
            {"role": "system", "content": "Given the search evidence, judge the "
             "claim. SUPPORTED only if evidence clearly confirms it. REFUTED only "
             "if the evidence DIRECTLY CONTRADICTS the claim — it affirmatively "
             "states something incompatible with it. Evidence that is silent, "
             "tangential, partial, or merely FAILS TO CONFIRM the claim is NOT "
             "refutation → UNVERIFIABLE. Never refute from your own memory or "
             "priors alone; judge only what this evidence shows. Reply ONLY JSON: "
             '{"verdict":"SUPPORTED|REFUTED|UNVERIFIABLE","why":"..."}.'},
            {"role": "user", "content": f"Claim: {claim}\n\nSearch evidence:\n{ev[:12000]}"},
        ], max_tokens=2048)
        v = str(parse_json_loose(j, {}).get("verdict", "UNVERIFIABLE")).upper()
        results.append({"claim": claim, "verdict": v})

    return fc_aggregate(results,
                        lambda c: _claim_load_bearing(question, report, c))


def _claim_load_bearing(question: str, report: str, claim: str) -> bool:
    """W0.1 承重仲裁(仅单驳时调用一次,成本≈一次判官调用):被驳 claim 若写错/
    删掉,报告对题目的核心结论会不会变?外围背景/举例/旁支细节 = 非承重。
    判官失败或输出不明 → 默认承重(保守 = 维持毙),绝不因判官抖动放行。"""
    ctx = report[:4000] + ("\n…\n" + report[-4000:] if len(report) > 8000 else "")
    j = judge_chat([
        {"role": "system", "content":
         "A factcheck flagged ONE claim inside a research report. Decide whether "
         "that claim is LOAD-BEARING: if it were wrong or removed, would the "
         "report's core answer/conclusion to the task change? Peripheral "
         "background, examples and side details are NOT load-bearing. Reply ONLY "
         'JSON: {"load_bearing": true|false, "why": "..."}'},
        {"role": "user", "content": f"Task: {question}\n\nFlagged claim: {claim}"
                                    f"\n\nReport:\n{ctx}"},
    ], max_tokens=1024)
    lb = parse_json_loose(j, {}).get("load_bearing")
    return lb if isinstance(lb, bool) else True


def fc_aggregate(results: list[dict], load_bearing_fn=None) -> dict:
    """聚合 claim 级三态 → 报告级 verdict。W0.1 治本(判官审计 20260714:单票否决
    在高 rubric 答案上误驳率 57%,吃掉 ~14% DR 产能):毙权收紧 ——
      refuted(=veto)⇔ ≥2 claim 被驳,或唯一被驳 claim 承重;
      单驳非承重 → mixed_minor_refuted(不毙:06/07 的 == "refuted" 检查天然放行,
      07 _fab_demote 的 != "supported" 检查天然保守降级,两侧语义都对);
      其余沿旧口径:全 unverifiable → unverifiable,否则 supported。
    load_bearing_fn=None(golden 桩缺席/离线回放)时单驳视同承重 = 旧行为,保守。"""
    verds = [r["verdict"].lower() for r in results]
    n_ref = sum(1 for v in verds if v == "refuted")
    ref_lb = None
    if n_ref == 1 and load_bearing_fn is not None:
        bad = next(r for r in results if r["verdict"].lower() == "refuted")
        ref_lb = load_bearing_fn(bad["claim"])
        bad["load_bearing"] = ref_lb
    if n_ref >= 2 or (n_ref == 1 and (ref_lb is None or ref_lb)):
        verdict = "refuted"
    elif n_ref == 1:
        verdict = "mixed_minor_refuted"
    elif verds and all(v == "unverifiable" for v in verds):
        verdict = "unverifiable"
    else:
        verdict = "supported"
    n_checked = sum(1 for v in verds if v in ("supported", "refuted"))
    return {"verdict": verdict,
            "n_supported": sum(1 for v in verds if v == "supported"),
            "n_refuted": n_ref,
            "n_unverifiable": sum(1 for v in verds if v == "unverifiable"),
            "refuted_load_bearing": ref_lb,
            "checked": n_checked, "results": results}


def _parse_json(s: str, default: dict) -> dict:
    if not s or s.startswith("__JUDGE_ERROR__"):
        return {**default, "raw": s}
    txt = re.sub(r"```(?:json)?|```", "", s).strip()  # strip code fences
    # 1) whole-string parse
    try:
        d = json.loads(txt)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    # 2) scan for balanced {...} objects, prefer the LAST (final verdict after CoT)
    found = []
    depth = start = 0
    for i, c in enumerate(txt):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}" and depth:
            depth -= 1
            if depth == 0:
                found.append(txt[start:i + 1])
    for cand in reversed(found):
        try:
            d = json.loads(cand)
            if isinstance(d, dict):
                return d
        except Exception:
            continue
    return {**default, "raw": s[:200]}


def _is_empty_answer(ans) -> bool:
    """final_answer 剥 think(含未闭合)后无实质正文 = 模型没给答案。
    严格空判据:只命中零非空白正文,不误伤 "D"/数字 这类超短真答案。"""
    s = re.sub(r"<think>.*?</think>", "", ans or "", flags=re.S)
    cut = s.find("<think>")
    if cut >= 0:
        s = s[:cut]
    return not s.strip()


_SCORER_CALIBER = "dr@3.2/1"
"""判分口径的**语义**版本。判分行为变了就 +1,改注释/重构不动它。

★ 20260814 dr@3.2 新增。为什么必须有:`Raven-X/bench/score.py` **在 `tree_sha` 之外**
(`build_identity.py` 只覆盖 `raven/`)⇒ 改判分器**不动任何指纹、不触发任何 bump**,
而它直接决定分数。唯一的看门人 `iso_manifest.json:bench_tree_sha` 只从 dr29 起才有,
是**批内**防篡改而不是**跨批**口径章 —— 它已经在 dr29→dr30 之间不同过,没有任何门响。
盘上现存 16 个 run 目录 / 48 条臂 / 2,406 行 HLE 判分行,没有一行能说出自己是哪把尺子量的。

⚠️ 语义章与机械章**必须成对**(见 `scorer_identity`):只盖手写号,有人忘改就静默漂移;
只盖文件哈希,改个注释就报"换尺子了" ⇒ 每批都响 ⇒ 训练所有人无视它。两个一起盖才分得开
「改了判分行为」和「改了注释」。这与 `drFlow.version` + `tree_sha` 是同一套纪律。
"""


def scorer_identity() -> dict:
    """本次判分用的尺子。逐行落盘 —— 报表不落盘、rescore 会改行,只有行本身跟得住。"""
    import hashlib                                            # noqa: PLC0415
    import pathlib                                            # noqa: PLC0415
    src = pathlib.Path(__file__).resolve()
    try:
        h = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
    except OSError:                       # 读不到自己就如实说,不许静默省略
        h = "unreadable"
    return {"caliber": _SCORER_CALIBER, "file": src.name, "file_sha256": h}


def score_row(r: dict, args) -> dict:
    """判分一行,并在**唯一的边界**盖上判分口径章。

    ★ 盖在这里而不是在 `_score_row` 的四个 return 上:四个 return 是四次忘记的机会,
    而"新增一个分支时忘了同步"正是本项目反复栽的形状。边界只有一个。
    """
    out = _score_row(r, args)
    out["scored_by"] = scorer_identity()
    return out


def _score_row(r: dict, args) -> dict:
    """Score one trajectory (thread-safe: no shared state, returns a fresh dict).
    All the cost lives here — blocking judge_chat / serper_search network calls —
    so rows can be fanned out across a ThreadPoolExecutor for near-linear speedup."""
    r = dict(r)  # never mutate the caller's row (threads share the input list)
    ans, gold = r["final_answer"], r.get("gold_answer", "")
    evidence_hit = bool(r.get("n_fetch", 0) > 0)  # answer saw a fetched page
    # C11 打分器路由(20260727):新目标套一批里混着三条打分通路,只有 exact 题有
    # gold_answer——table 的 gold 在 gold_table(走 scorer_widesearch)、report 无 gold
    # (走 scorer_drb)。本函数只负责 exact。不分流的话这 300 题(1430 的 21.0%)会因
    # "gold 为空"被判错,headline 系统性偏低**且偏低幅度随三轴配比漂移**。
    # 标 skipped 而不是丢行:丢行会让它们静默出分母,那比判错更危险(见判官态设计规则 4)。
    scorer = r.get("scorer") or "exact"
    if scorer != "exact":
        return {**r, "verdict": "skipped", "judge": {"method": f"routed_to:{scorer}"},
                "evidence_hit": evidence_hit}
    if _is_empty_answer(ans):
        # 空答护栏(T0.1):剥 think 后无正文 = 模型没给答案,喂判官会幻觉判对
        # (八源实测 A 臂 96 空答里 22 条判 correct,连 gold 都不在轨迹)。直接
        # incorrect、不喂判官。这是真 incorrect(任务失败)非 judge_failed,须计入统计。
        return {**r, "verdict": "incorrect",
                "judge": {"method": "empty_answer"},
                "evidence_hit": evidence_hit}
    ev = evidence_text(r.get("trajectory", []), args.max_evidence_chars)
    if gold_is_corrupt(gold):
        # 损坏 gold 拦截(PROTO-02):不给裁判当参照;DS 的 match/judge 对垃圾 gold
        # 也无意义 → 标记 gold_corrupt,让下游按无 gold 处理/送 oracle 修复。
        r["gold_corrupt"] = True
    if r["type"] == "DS":
        if r.get("gold_corrupt"):
            # 审计 minor 落地:损坏 gold 下 match/judge 无参照,判决不可信 →
            # judge_failed(06 会同时排除出 correct 与 RQ1 失败池),送 gold 重导。
            r["judge_failed"] = True
            return {**r, "verdict": "incorrect",
                    "judge": {"method": "gold_corrupt", "judge_failed": True},
                    "evidence_hit": evidence_hit}
        if match_ds(gold, ans):
            # match 是纯字符串路径,不经判官 —— 身份仍要记,好让下游能区分
            # "判官判的" 与 "字符串匹配判的"(两者的噪声地板完全不同)。
            verdict, judge = "correct", {"method": "match", "identity": None}
        else:
            j = judge_ds(r["question"], gold, ans)
            # ★ 20260803 起判官身份必须落进产物(`judge_chat` 按 JUDGE_BACKEND 分派、
            # 默认 sonnet,而我们的臂靠 eval_demo.sh 硬编码 qwen)。20260813 起这一行
            # 由 `_judged` 自己盖 —— 见那里的注释:写在这里意味着每个新调用方都要
            # 记得,而下一个调用方(rescore_short_gold.py)恰好没记得。
            correct_llm = bool(j.get("correct"))
            # B3 一致性否决:布尔=correct 但 why 自述"不匹配" → 判决不可信,
            # 记 judge_failed(06 会把它同时排除出 correct 与 RQ1 负样本)。
            if correct_llm and _JUDGE_CONTRA_RE.search(str(j.get("why") or "")):
                j["contradiction_veto"] = True
                j["judge_failed"] = True
                correct_llm = False
            verdict = "correct" if correct_llm else "incorrect"
            # W0.3b(pilot v1 校准):认输通道扩到 DS——browsecomp 式失败本就是
            # 短答案题;难题池 62% DS,只接 DR 会漏掉主战场(v1 实测 0 命中主因之一)。
            if GIVEUP_MODE and verdict == "incorrect" and not j.get("judge_failed"):
                gu = giveup_check(r, ans, ev)   # ev 在分支前已算好(evidence_text)
                j["giveup"] = gu
                if gu["pass"]:
                    verdict = "honest_giveup"
            judge = {"method": "llm", **j}
        # ④ per-hop coverage computed HERE (was ⑥) so ⑤ can gate on it
        r["hop_coverage"] = (hop_coverage(r["question"], ev, r.get("hop"))
                             if ev else {"coverage": None, "note": "no evidence"})
    else:  # DR
        gold_ref = "" if r.get("gold_corrupt") else gold   # 裁判降级为无参照打分
        j = judge_dr(r["question"], ans, gold_ref)
        if args.no_factcheck:
            fc = {"verdict": "skipped", "note": "--no-factcheck"}
        else:
            fc = factcheck_dr(r["question"], ans, args.factcheck_claims,
                              args.factcheck_fetch_pages)
        # 盲区②仲裁:rubric 低分且判词呈"事实不信"语式,而 live factcheck 证据侧
        # SUPPORTED(≥1 supported、0 refuted)→ 判词疑为过时先验压证据,用证据
        # 加持的提示重判一次,采用重判结果(留双份记录供追溯)。
        if (j.get("avg", 0) < 6.0 and not j.get("judge_failed")
                and fc.get("verdict") == "supported"
                and (fc.get("n_supported") or 0) >= 1
                and _DISBELIEF_RE.search(str(j.get("why") or ""))):
            j2 = judge_dr(r["question"], ans, gold_ref, evidence_note=_EVIDENCE_NOTE)
            if not j2.get("judge_failed"):
                j2["arbitrated"] = True
                j2["first_pass"] = {"avg": j.get("avg"),
                                    "why": str(j.get("why") or "")[:200]}
                j = j2
        avg_ok = j.get("avg", 0) >= 6.0
        j["factcheck"] = fc
        # ③ a refuted fact VETOES the report no matter how well it scores
        verdict = "correct" if (avg_ok and fc["verdict"] != "refuted") else "incorrect"
        # W0.3 认输通道(GIVEUP_MODE 批次专用):判 incorrect 的 DR 追加三查,
        # 全过 → honest_giveup(教"预算耗尽→体面收尾";correct 行不走此路)。
        if GIVEUP_MODE and verdict == "incorrect" and not j.get("judge_failed"):
            gu = giveup_check(r, ans, ev)
            j["giveup"] = gu
            if gu["pass"]:
                verdict = "honest_giveup"
        judge = {"method": "rubric4+factcheck", **j}
    if judge.get("judge_failed"):
        # 判决不可信(双次解析失败/评分对象缺失):verdict 保守记 incorrect,但打标
        # 供 06 排除出 RQ1 负样本与统计(判决噪声 ≠ 已验证的失败)。
        r["judge_failed"] = True
    return {**r, "verdict": verdict, "judge": judge, "evidence_hit": evidence_hit}


def _progress(k: int, n: int, s: dict) -> str:
    if s["type"] == "DR":
        extra = f" fact={s['judge'].get('factcheck', {}).get('verdict', '-')}"
    elif s.get("hop_coverage", {}).get("coverage") is not None:
        extra = f" hop_cov={s['hop_coverage']['coverage']}"
    else:
        extra = ""
    return (f"[{k:>3}/{n}] {s['qid']:>16} {s['type']} "
            f"{s['verdict']:>9} evidence_hit={s['evidence_hit']}{extra}")


def golden_tests() -> int:
    """W0.1 golden:fc_aggregate 毙权口径回归(改聚合/承重逻辑后必跑;零网络)。
    依据:判官审计_factcheck单票否决误驳率_20260714.md(单票否决误驳 57%)。"""
    def R(*vs):
        return [{"claim": f"c{i}", "verdict": v} for i, v in enumerate(vs)]
    cases = [
        ("双驳必毙",           R("REFUTED", "REFUTED", "SUPPORTED"), lambda c: False, "refuted"),
        ("单驳且承重毙",       R("REFUTED", "SUPPORTED"),            lambda c: True,  "refuted"),
        ("单驳非承重放行",     R("REFUTED", "SUPPORTED", "UNVERIFIABLE"), lambda c: False, "mixed_minor_refuted"),
        ("单驳无仲裁按承重毙", R("REFUTED", "SUPPORTED"),            None,            "refuted"),
        ("全不可核不毙",       R("UNVERIFIABLE", "UNVERIFIABLE"),    lambda c: True,  "unverifiable"),
        ("有支持无驳",         R("SUPPORTED", "UNVERIFIABLE"),       lambda c: True,  "supported"),
    ]
    ok = True
    for name, res, fn, want in cases:
        got = fc_aggregate(res, fn)["verdict"]
        mark = "✅" if got == want else "❌"
        ok &= got == want
        print(f"  {mark} golden {name}: got={got} want={want}")
    # 下游语义断言:mixed_minor_refuted 既不触发 06/07 的毙(== refuted),也不冒充
    # supported(07 _fab_demote / 06 盲区②仲裁都检查 == supported);细节字段留痕。
    m = fc_aggregate(R("REFUTED", "SUPPORTED"), lambda c: False)
    ok &= (m["verdict"] not in ("refuted", "supported")
           and m["refuted_load_bearing"] is False and m["n_refuted"] == 1
           and m["results"][0].get("load_bearing") is False)
    print(f"  {'✅' if ok else '❌'} golden 下游语义(≠refuted 且 ≠supported,承重留痕)")

    # --- W0.3 认输三查 golden(judge_fn 注入桩,零网络) ---
    hedged = ("排查了七位候选人,均有一条硬约束不满足。基于现有证据,最有依据的"
              "候选是 X,但检索到的资料无法确认其获奖年份,此结论不确定。")
    confident = "答案就是 X,毫无疑问,证据充分完整,可以确认。"
    stub_yes = lambda msgs, **kw: ('{"candidate_grounded":true,'
                                   '"uncertainty_explicit":true,'
                                   '"no_loop_or_filler":true,"why":"ok"}')
    stub_err = lambda msgs, **kw: "__JUDGE_ERROR__: boom"
    g_cases = [
        ("尽职+三查过=pass", {"n_search": 12, "n_fetch": 3}, hedged, stub_yes, True),
        ("搜得少不配认输",   {"n_search": 3, "n_fetch": 3},  hedged, stub_yes, False),
        ("没读页不配认输",   {"n_search": 12, "n_fetch": 0}, hedged, stub_yes, False),
        ("结尾无hedge不过",  {"n_search": 12, "n_fetch": 3}, confident, stub_yes, False),
        ("判官挂了保守不过", {"n_search": 12, "n_fetch": 3}, hedged, stub_err, False),
    ]
    for name, rr, ans, stub, want in g_cases:
        got = giveup_check({**rr, "question": "q"}, ans, "evidence...", judge_fn=stub)["pass"]
        good = got == want
        ok &= good
        print(f"  {'✅' if good else '❌'} golden 认输 {name}: pass={got} want={want}")
    print("=== 06 factcheck+giveup golden:", "ALL PASS" if ok else "FAILED", "===")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", action="store_true",
                    help="run offline golden tests for the W0.1 factcheck veto "
                         "aggregation, then exit (no network / no judge needed)")
    ap.add_argument("--no-factcheck", action="store_true",
                    help="skip the ③ online fact-check axis (cheap/offline runs)")
    ap.add_argument("--factcheck-claims", type=int, default=3)
    ap.add_argument("--factcheck-fetch-pages", type=int, default=1,
                    help="fetch top-N result pages (Jina) as fact-check evidence; "
                         "0 = snippet-only (quest-20 曾被 snippet-only 误杀)")
    ap.add_argument("--max-evidence-chars", type=int, default=30000)
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel judge workers (I/O-bound network calls; 8-12 is safe "
                         "vs OpenRouter/Serper rate limits). 1 = sequential.")
    args = ap.parse_args()
    if args.golden:
        raise SystemExit(golden_tests())
    from common import require_judge_key
    require_judge_key()   # 缺 key 快败:否则 DR 全 judge_failed、DS 回退判全默认 incorrect

    rows = read_jsonl(DATA / "traj_clean.jsonl")
    n = len(rows)
    scored = [None] * n
    if args.workers <= 1:
        for i, r in enumerate(rows):
            scored[i] = score_row(r, args)
            print(_progress(i + 1, n, scored[i]), flush=True)
    else:
        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(score_row, r, args): i for i, r in enumerate(rows)}
            for fut in as_completed(futs):
                i = futs[fut]
                scored[i] = fut.result()  # propagate exceptions (fail loud, not silent)
                done += 1
                print(_progress(done, n, scored[i]), flush=True)

    write_jsonl(DATA / "traj_scored.jsonl", scored)
    from collections import Counter
    v = Counter(s["verdict"] for s in scored)
    by_src, by_lang = {}, {}
    for s in scored:
        by_src.setdefault(s["source"], Counter())[s["verdict"]] += 1
        by_lang.setdefault(s.get("lang") or "?", Counter())[s["verdict"]] += 1
    fc_veto = sum(1 for s in scored if s["type"] == "DR"
                  and s["judge"].get("factcheck", {}).get("verdict") == "refuted")
    fc_minor = sum(1 for s in scored if s["type"] == "DR"
                   and s["judge"].get("factcheck", {}).get("verdict") == "mixed_minor_refuted")
    # ★★ 20260813 Framework:**判分计数不再落进这个文件。**
    # 它们曾以 `verdict` / `by_source` / `by_lang` 三块落盘,而 `rescore_short_gold.py`
    # 事后会翻转 `traj_scored.jsonl` 里的行、**只往同一个文件补一个 `rescore_short_gold`
    # 小节,不更新这三块** ⇒ 落盘的 `verdict` 是 rescore **之前**的快照。
    # 实测 eval_web_dr30_20260812/run_base:文件里写 correct=161,而现行 scored 是 130。
    # 读者没有任何办法从文件本身分辨它是当前值还是快照。
    #
    # 为什么是删而不是修:全树**没有一个消费者**读这三块(逐一核过 —— 每个报表脚本
    # 都从 `traj_scored.jsonl` 的逐行 `verdict` 自己算)。所以它是一份**只写不读、
    # 却会过期**的缓存。修它意味着以后每个后处理脚本都得记得同步它,而下一个忘记的人
    # 会再造一次同样的假象;删掉之后"权威计数只有一个来源"变成结构性的。
    # 计数仍然在下面打印 —— 打印是当次运行的观察,不是会被别人引用的落盘事实。
    rep = {"n": len(scored),
           "_counts_note": ("判分计数不落盘:rescore 等后处理会改 traj_scored.jsonl 而"
                            "不改本文件,落盘的计数必然过期。权威计数请从 "
                            "traj_scored.jsonl 逐行 verdict 现算。"),
           "evidence_hit": sum(1 for s in scored if s["evidence_hit"]),
           "dr_factcheck_vetoed": fc_veto,
           "dr_factcheck_minor_refuted": fc_minor,   # W0.1:单驳非承重放行数(误驳回收观测口)
           "judge_failed": sum(1 for s in scored if s.get("judge_failed")),
           "gold_corrupt": sum(1 for s in scored if s.get("gold_corrupt")),
           # exact 轴的分母必须显式:routed_out 是分流给 table/report 打分器的题,
           # 它们既不算对也不算错,不能混进 exact 的 accuracy。
           # 这两个是**路由**的记账不是判分的:`skipped` 由打分器分流决定,rescore
           # 不会改它 ⇒ 不共享上面那个过期风险,保留落盘。
           "n_exact": sum(1 for s in scored if s["verdict"] != "skipped"),
           "routed_out": sum(1 for s in scored if s["verdict"] == "skipped")}
    (DATA / "validate_report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2))
    print("\n=== validate ===\n" + json.dumps(rep, ensure_ascii=False, indent=2))
    # 计数只打印。**故意不落盘** —— 见上面 rep 的注释。
    print("\n=== 本次判分计数(只打印,不落盘;权威值请从 traj_scored.jsonl 现算)===")
    print("  verdict   " + json.dumps(dict(v), ensure_ascii=False))
    print("  by_source " + json.dumps({k: dict(c) for k, c in by_src.items()},
                                      ensure_ascii=False))
    print("  by_lang   " + json.dumps({k: dict(c) for k, c in by_lang.items()},
                                      ensure_ascii=False))


if __name__ == "__main__":
    main()
