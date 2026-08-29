"""Chinese language data the engines consult: cue words, punctuation, legacy headers.

Not translations (those are ``raven.i18n.zh``): these are the Chinese forms the
scent detector, the sentinel and the parsers recognise in what a Chinese user
writes, and the Chinese spellings of attention.md headers that older files still
carry. Keeping them here keeps every other module free of CJK text and makes
the language-specific surface of each engine visible in one place.
"""

FUNCTION_WORDS: tuple[str, ...] = (
    "帮我",
    "给我",
    "请你",
    "麻烦",
    "一下",
    "一个",
    "这个",
    "那个",
    "可以",
    "继续",
    "接着",
    "好的",
    "好了",
    "谢谢",
    "不对",
    "重新",
    "再来",
    "然后",
)
"""Function words and interjections that carry no retrievable intent (scent detector)."""

ANAPHORA_PREFIXES: tuple[str, ...] = (
    "这",
    "那",
    "它",
    "他",
    "她",
    "再",
    "还",
    "也",
    "又",
    "不对",
    "换",
    "改",
    "继续",
    "接着",
)
"""Openers that refer back to something already in the conversation."""

TASK_VERBS: tuple[str, ...] = (
    "帮我",
    "写",
    "查",
    "找",
    "分析",
    "生成",
    "对比",
    "整理",
    "做",
    "搭",
    "修",
    "调研",
    "统计",
    "翻译",
    "总结",
)
"""Verbs that open a task request."""

AUTO_TAG_STOPWORDS: frozenset[str] = frozenset(
    {
        "你",
        "我",
        "的",
        "了",
        "是",
        "和",
        "或",
        "在",
        "有",
        "要",
        "对",
        "也",
        "都",
        "就",
        "但",
        "可以",
        "可能",
        "应该",
        "需要",
        "还是",
        "如果",
        "因为",
        "所以",
        "提醒",
        "记得",
        "建议",
        "注意",
        "另外",
        "顺便",
        "另",
        "今天",
        "明天",
        "昨天",
        "最近",
        "马上",
    }
)
"""Words dropped when the sentinel derives a topic tag from a nudge."""

FUNCTIONAL_WORDS: frozenset[str] = frozenset(
    {
        "的",
        "了",
        "在",
        "是",
        "我",
        "有",
        "和",
        "就",
        "不",
        "人",
        "都",
        "一",
        "也",
        "要",
        "去",
        "会",
        "着",
        "到",
        "上",
        "下",
        "说",
        "用户",
        "他",
        "她",
    }
)
"""Common functional words the routine learner skips when extracting keywords."""

QUIET_KEYWORDS: tuple[str, ...] = (
    "安静时段",
    "勿扰",
    "不打扰",
    "免打扰",
    "安静时间",
)
"""Ways a user names quiet hours in their preferences."""

DIRECTIVE_MARKERS: tuple[str, ...] = (
    "请",
    "别",
    "不要",
    "应该",
    "总是",
    "永远",
)
"""Prefixes that mark an inbound message as a standing instruction."""

YES_WORDS: tuple[str, ...] = ("是", "确认", "好", "嗯", "对")
NO_WORDS: tuple[str, ...] = ("否", "取消", "不", "算了")
TITLE_LABELS: tuple[str, ...] = ("标题", "会话标题")

COLON_CLASS = "[:：]"
"""ASCII or fullwidth colon, as a regex character class."""
SENTENCE_ENDS = "！。"
"""Fullwidth sentence-ending punctuation, for a regex character class."""
PUNCTUATION_MARKS = "。，！？"
"""Fullwidth stops, commas and marks, for a regex character class."""
RANGE_MARKS = "～至到"
"""Range connectors a Chinese writer puts between two times."""
MONTH = "月"
DAY = "日"
DATE_PATTERN = r"(?P<m>\d{1,2})月(?P<d>\d{1,2})[日号]"
"""A month/day date written with the Chinese counters."""

LANGUAGE_NAME = "Simplified Chinese (简体中文)"

LEGACY_ATTENTION_HEADERS: dict[str, str] = {
    "## 用户指令": "## User overrides",
    "## 活跃话题": "## Active threads",
    "## 下一步预测": "## Predicted next 3 days",
    "## 最近放弃": "## Recently abandoned, worth resuming",
    "## 项目节奏": "## Project rhythm (last 7 days)",
    "## 当前聚焦": "## Currently focused on",
    "## 跨项目活跃话题(14天)": "## Cross-project behavior patterns (14d)",
    "## 值得续作的已放弃": "## Recently abandoned, worth resuming",
    "## 最近主动决策(14天)": "## Recent proactive decisions (14d)",
    "## 未来3日预测": "## Predicted next 3 days",
    "## 项目节奏(7天)": "## Project rhythm (last 7 days)",
    "## 近期立场日志(30天)": "## Recent stance log (30d)",
    "## 待处理提议": "## Pending proposals",
    "## 已拒绝提议(冷却中)": "## Rejected proposals (cooldown)",
    "## 已归档模式": "## Archived patterns",
    "## 今日 fire 计划": "## Today's fire plan",
}
"""attention.md H2 headers as older files spell them, mapped to the canonical English ones."""

TOOL_SEARCH_QUERY_EXAMPLE = "生成图片"
"""A tool-search query in Chinese, shown beside an English one so the model
recognises either phrasing."""
TITLE_EXAMPLE = "修复登录跳转"
"""A conversation title in Chinese, paired with its English form in the
title tool's parameter description."""

TRIGGER_STOPWORDS: frozenset[str] = frozenset(
    {
        "帮我",  # bang wo: "help me"
        "给我",  # gei wo: "give me"
        "我要",  # wo yao: "I want"
        "我想",  # wo xiang: "I'd like"
        "一个",  # yi ge: "a/one"
        "一下",  # yi xia: "briefly"
        "一份",  # yi fen: "a copy of"
        "这个",  # zhe ge: "this"
        "那个",  # na ge: "that"
        "什么",  # shen me: "what"
        "怎么",  # zen me: "how"
        "可以",  # ke yi: "can/may"
        "需要",  # xu yao: "need"
        "麻烦",  # ma fan: "please/trouble you"
        "模板",  # mu ban: "template"
        "流程",  # liu cheng: "process/flow"
        "方案",  # fang an: "plan/scheme"
        "自动化",  # zi dong hua: "automation"
        "工作流",  # gong zuo liu: "workflow"
    }
)
"""Openers and mechanism words a Chinese request carries whatever it asks for,
so a playbook trigger built from them would fire on every request."""

TASK_TITLE_EXAMPLE = "草拟回复"
"""An imperative task title in Chinese, shown beside its English form so the
model writes a title in the user's language rather than translating one."""
