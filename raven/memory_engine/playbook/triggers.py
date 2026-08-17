"""Offline trigger expansion — build a playbook's L1 match vocabulary.

Runs once per playbook at compile time (never per user message): one LLM
call expands the description, the user's original wording and any author
seeds into candidate keywords/phrases, then two automatic guards cut what
would poison the L1 funnel:

- **rule filter** — too-short entries and bare function words;
- **generic-word filter** — the load-bearing one. A candidate is matched
  against a corpus of unrelated everyday messages exactly the way L1 will
  match it; hitting more than ``max_hit_rate`` of them means the word is
  generic (an IDF argument), and a generic entry costs an LLM gate call on
  every message that contains it, forever. Filtering is deliberately
  strict: a dropped word shows up as an observable miss and can be
  re-added, a generic word is an invisible standing cost.

Cross-playbook collision checking is a separate pure function over the
whole library (`find_collisions`), run at load/compile time by the caller.
"""

from __future__ import annotations

import json
import unicodedata
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.memory_engine.playbook.types import Triggers

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider

_EXPAND_TOOL_NAME = "emit_triggers"

_MIN_KEYWORD_CHARS = 2
_DEFAULT_MAX_HIT_RATE = 0.02

# Bare function words that survive the length rule but carry no task signal.
# Domain-generic words ("文章", "报告") are NOT listed here on purpose — the
# negative-sample filter judges those from data, not from anyone's intuition.
_STOPWORDS = {
    "帮我",
    "给我",
    "我要",
    "我想",
    "一个",
    "一下",
    "一份",
    "这个",
    "那个",
    "什么",
    "怎么",
    "可以",
    "需要",
    "麻烦",
    "the",
    "and",
    "for",
    "with",
    "make",
    "help",
    # Mechanism words: they describe how the system works, not any one task,
    # so they appear in requests for every playbook and trigger all of them.
    "playbook",
    "workflow",
    "pipeline",
    "template",
    "framework",
    "process",
    "模板",
    "流程",
    "方案",
    "自动化",
    "工作流",
}

_EXPAND_PROMPT = """\
你在为一个任务模板（playbook）建立触发词表。用户消息中出现这些词/短语时，该 playbook 会成为候选。

Playbook 意图：{description}
当初的用户原话：{source_input}
已有种子词：{seeds}

核心方法：想象 20 个不同的用户想要**这个任务的结果**时会怎么开口——他们说的是想要的效果
（"发条推""帮我看看用户都在吐槽什么""对比一下A和B"），不是任务的学名（"内容生产流水线"）。
从这些开口方式里提词。

沿五个维度扩充，产出 20~40 个 keywords（单词和短语都放这一个列表）：
1. 动作短语（最重要）："动词+宾语"的短组合，如"发推""写周报""对比竞品"——
   两三个字的动作组合是用户最常说的形态。**每个动作短语同时给出量词插入变体**：
   "发推"→同时收"发条推""发个推"；"写周报"→"写份周报"（中文动宾之间常插量词，
   子串匹配跨不过去，必须显式列出）；
2. 同义近义词（上位词最多取一层，宁可不取）；
3. 口语化说法（含吐槽/抱怨/夸赞等情绪化表达，如果任务与之相关）；
4. 中英对照（用户常混用英文术语，如 twitter/tweet 与 推特/推文 并收）；
5. 强指向的场景词。

硬性要求：
- 禁收机制词：playbook、workflow、模板、流程、pipeline、方案、自动化——这些描述的是
  "怎么做"的机制，任何任务的用户原话里都可能出现，不指向本任务；
- 只收专指性强的词；"文章""报告""数据"这类日常高频词绝对不收；
- 短语要像用户真实说话的片段，不要完整句子；
- 全部小写。"""


def normalize(text: str) -> str:
    """The one normalization both vocabulary and query go through.

    NFKC folds full-width forms, lowercase folds case, whitespace collapses
    to single spaces. Chinese is substring-matched without segmentation, so
    no tokenizer is involved anywhere.
    """
    folded = unicodedata.normalize("NFKC", text).lower()
    return " ".join(folded.split())


def _expand_tool() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": _EXPAND_TOOL_NAME,
                "description": "Submit the expanded trigger vocabulary.",
                "parameters": {
                    "type": "object",
                    "properties": {"keywords": {"type": "array", "items": {"type": "string"}}},
                    "required": ["keywords"],
                },
            },
        }
    ]


def _rule_filter(entries: list[str], min_chars: int) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for raw in entries:
        entry = normalize(str(raw))
        if len(entry.replace(" ", "")) < min_chars or entry in _STOPWORDS or entry in seen:
            continue
        seen.add(entry)
        kept.append(entry)
    return kept


def generic_hit_rate(entry: str, negative_samples: list[str]) -> float:
    """Fraction of unrelated messages this entry would (mis)match."""
    if not negative_samples:
        return 0.0
    hits = sum(1 for msg in negative_samples if entry in msg)
    return hits / len(negative_samples)


def _generic_filter(
    entries: list[str], negative_samples: list[str], max_hit_rate: float
) -> tuple[list[str], list[str]]:
    normalized_samples = [normalize(m) for m in negative_samples]
    kept, dropped = [], []
    for entry in entries:
        if generic_hit_rate(entry, normalized_samples) > max_hit_rate:
            dropped.append(entry)
        else:
            kept.append(entry)
    return kept, dropped


async def _expand_once(
    provider: "LLMProvider",
    description: str,
    source_input: str,
    seeds: list[str],
    model: str | None,
) -> list[str]:
    response = await provider.chat_with_retry(
        messages=[
            {
                "role": "user",
                "content": _EXPAND_PROMPT.format(
                    description=description,
                    source_input=source_input or "（无）",
                    seeds=json.dumps(seeds, ensure_ascii=False),
                ),
            }
        ],
        tools=_expand_tool(),
        model=model,
        tool_choice={"type": "function", "function": {"name": _EXPAND_TOOL_NAME}},
    )
    if not response.has_tool_calls:
        return []
    args = response.tool_calls[0].arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if not isinstance(args, dict):
        return []
    return [*(args.get("keywords") or []), *(args.get("phrases") or [])]


async def expand_triggers(
    provider: "LLMProvider",
    *,
    description: str,
    source_input: str = "",
    seeds: Triggers | None = None,
    negative_samples: list[str] | None = None,
    max_hit_rate: float = _DEFAULT_MAX_HIT_RATE,
    rounds: int = 2,
    model: str | None = None,
) -> Triggers:
    """Expansion calls plus the guard pipeline; returns the vocabulary
    ready for human review.

    ``rounds`` samples the expansion more than once and unions the results:
    a single sample is high-variance (a word present in one run vanishes in
    the next), and recall lost at L1 is unrecoverable downstream, while an
    extra entry only costs a filtered candidate. Union first, guard after.
    Seeds are kept unless a guard drops them — the guards outrank the
    author, because a generic seed hurts the same as a generic expansion."""
    seed_words = list(seeds.keywords) if seeds is not None else []
    raw: list[str] = []
    for _ in range(max(1, rounds)):
        raw += await _expand_once(provider, description, source_input, seed_words, model)
    return guard_triggers(
        seed_words + raw,
        negative_samples=negative_samples,
        max_hit_rate=max_hit_rate,
        what=description[:60],
    )


class TriggerGuardError(ValueError):
    """Every candidate was dropped, so there is no vocabulary to index."""


def guard_triggers(
    entries: "list[str] | Triggers",
    *,
    negative_samples: list[str] | None = None,
    max_hit_rate: float = _DEFAULT_MAX_HIT_RATE,
    what: str = "",
) -> Triggers:
    """Run the guard pipeline over a vocabulary, without expansion calls.

    Public because the guards must apply to *every* path that fills the L1
    index, not only to expansion: a vocabulary proposed by a model or typed by
    an author reaches the same index and carries the same standing cost, so
    "the guards outrank the author" has to hold for whoever proposes the words.
    Callers that want expansion as well go through :func:`expand_triggers`,
    which ends here.

    Raises:
        `TriggerGuardError`: nothing survived. Reported rather than silently
            widened, because the alternative -- keeping a dropped entry so the
            playbook still has one -- reinstates exactly the entry a guard just
            judged unfit.
    """
    raw = list(entries.keywords) if isinstance(entries, Triggers) else [str(e) for e in entries]
    keywords = _rule_filter(raw, _MIN_KEYWORD_CHARS)
    if negative_samples:
        keywords, dropped = _generic_filter(keywords, negative_samples, max_hit_rate)
        if dropped:
            logger.info("trigger guards dropped generic entries: {}", dropped)
    else:
        # Said out loud because this is the load-bearing guard: the rule filter
        # only catches short and stop-word entries, so without a corpus to
        # measure hit rate against, a plausible-looking but generic word
        # survives -- and a generic L1 entry costs a gate call on every message
        # containing it, for as long as the playbook exists.
        logger.warning(
            "trigger guards for {!r} ran without negative samples: the generic-word filter "
            "was skipped, so the vocabulary passed only the length and stop-word rules",
            what or raw[:3],
        )
    if not keywords:
        raise TriggerGuardError(
            f"every trigger candidate was dropped by the guards (from {raw!r}); "
            "propose words that are specific to this task and at least "
            f"{_MIN_KEYWORD_CHARS} characters long"
        )
    return Triggers(keywords=keywords)


def find_collisions(library: dict[str, Triggers]) -> dict[str, list[str]]:
    """Map each vocabulary entry claimed by 2+ playbooks to the claimants.

    A collision is a warning, not an error: overlapping candidates simply
    go to the gate together. The report exists so a human can decide to
    sharpen one side's vocabulary -- which is why a playbook must not be able
    to collide with itself: ``["seo", "SEO"]`` normalizes to one entry claimed
    twice, and a report saying "a collides with a" gives the reader nothing to
    sharpen while burying the cross-playbook conflicts that do."""
    owners: dict[str, list[str]] = {}
    for pid, trig in library.items():
        for entry in sorted({normalize(e) for e in trig.keywords}):
            owners.setdefault(entry, []).append(pid)
    return {entry: pids for entry, pids in owners.items() if len(pids) > 1}
