"""Pull-mode skill discovery: fat/lean judgement, scent menu, find_skill."""

from raven.agent.loop.failure_streak import loop_break_nudge
from raven.agent.tools.skill_hub import FindSkillTool, ReadSkillTool
from raven.context_engine.assembler import ContextAssembler
from raven.context_engine.base import AssemblyContext
from raven.context_engine.scent import (
    ScentMenu,
    build_scent_query,
    is_fat,
    novelty,
    recent_user_window,
)
from raven.skill_hub.policy import SkillPolicy

WINDOW = "这周的数据帮我整理个周报\n先按渠道分组"


# ---------------------------------------------------------------- fat/lean


def test_interjections_and_short_turns_are_lean():
    for msg in ("好的", "继续", "嗯 好", "再来一次", "ok go"):
        assert not is_fat(msg, WINDOW), msg


def test_followup_with_anaphora_and_low_novelty_is_lean():
    assert not is_fat("再改短点吧这个周报", WINDOW)


def test_topic_switch_is_fat():
    assert is_fat("帮我看看这段 python 报错是怎么回事", WINDOW)


def test_task_verb_makes_a_contentful_turn_fat():
    assert is_fat("统计一下每个渠道的周报覆盖率", WINDOW)


def test_novelty_is_low_for_window_vocabulary():
    assert novelty("整理个周报", WINDOW) < 0.3
    assert novelty("python 栈溢出排查", WINDOW) > 0.7


# ---------------------------------------------------------------- query


def test_fresh_topic_queries_alone():
    q = build_scent_query("帮我看看这段 python 报错是怎么回事", WINDOW)
    assert "周报" not in q


def test_anaphoric_task_pulls_the_window_in():
    q = build_scent_query("再帮我查下它的分渠道数据", WINDOW)
    assert "周报" in q and "分渠道" in q


def test_recent_user_window_takes_last_two_user_messages():
    msgs = [
        {"role": "user", "content": "第一条"},
        {"role": "assistant", "content": "答"},
        {"role": "user", "content": "第二条"},
        {"role": "user", "content": "第三条"},
    ]
    w = recent_user_window(msgs)
    assert "第一条" not in w and "第二条" in w and "第三条" in w


# ---------------------------------------------------------------- menu


class _Hit:
    def __init__(self, qid, desc="", content=""):
        self.qualified_id = qid
        self.name = qid.split("/")[-1]
        self.content = content
        self.score = 1.0
        self.meta = {"description": desc}


class _Router:
    def __init__(self, hits=None, raise_=False):
        self.hits = hits or []
        self.raise_ = raise_
        self.queries = []

    async def select(self, query, history, k):
        self.queries.append(query)
        if self.raise_:
            raise RuntimeError("boom")
        return self.hits[:k]


async def test_menu_renders_ids_query_and_untrusted_markers():
    router = _Router([_Hit("local/sql-weekly-agg", "按周聚合的 SQL 模式"), _Hit("hub/report-structure", "周报结构")])
    res = await ScentMenu(router).build("统计一下每个渠道的数据覆盖率", [])
    assert "local/sql-weekly-agg" in res.text and "hub/report-structure" in res.text
    assert "matched for" in res.text
    assert "read_skill" in res.text and "find_skill" in res.text
    assert "UNTRUSTED" in res.text  # wrapped as data, not instructions
    assert res.skill_ids == ["local/sql-weekly-agg", "hub/report-structure"]


async def test_menu_is_silent_on_lean_turns_and_failures():
    router = _Router([_Hit("local/x", "d")])
    assert not await ScentMenu(router).build("好的", [])
    assert router.queries == []  # lean turn never touches the router
    assert not await ScentMenu(_Router(raise_=True)).build("统计一下渠道覆盖率数据", [])
    silent = await ScentMenu(_Router([])).build("统计一下渠道覆盖率数据", [])
    assert silent.text == "" and silent.skill_ids == []


async def test_menu_truncates_descriptions_to_one_line():
    router = _Router([_Hit("local/x", "第一行描述很有用\n第二行不该出现")])
    res = await ScentMenu(router).build("统计一下渠道覆盖率数据", [])
    assert "第一行描述很有用" in res.text and "第二行" not in res.text


async def test_menu_times_out_gracefully_on_a_slow_source():
    import asyncio

    class _Slow:
        async def select(self, query, history, k):
            await asyncio.sleep(10)

    assert not await ScentMenu(_Slow()).build("统计一下渠道覆盖率数据", [])


async def test_menu_enforces_blocklist_and_safety_bar():
    hits = [
        _Hit("hub/blocked-one", "should never be advertised"),
        _Hit("hub/low-safety", "score too low"),
        _Hit("hub/clean", "fine"),
    ]
    hits[1].meta["score_safety"] = 0.2
    policy = SkillPolicy.create(min_safety=0.7, blocklist=["blocked-one"])
    res = await ScentMenu(_Router(hits), policy=policy).build("统计一下渠道覆盖率数据", [])
    assert "hub/blocked-one" not in res.text
    assert "hub/low-safety" not in res.text
    assert res.skill_ids == ["hub/clean"]


async def test_menu_survives_the_session_persist_strip():
    # AgentLoop._save_turn keeps only what follows the envelope's first
    # blank line, so the menu must live inside the first paragraph — a
    # persisted menu would render as the user's own words on resume and
    # feed the next turn's novelty window its own output.
    router = _Router([_Hit("local/sql-weekly-agg", "按周聚合的 SQL 模式")])
    res = await ScentMenu(router).build("统计一下每个渠道的数据覆盖率", [])
    assert "\n\n" not in res.text

    assembler = ContextAssembler([], lambda: [])
    ctx = AssemblyContext(
        session_key="s",
        current_message="统计一下每个渠道的数据覆盖率",
        media=None,
        channel=None,
        chat_id=None,
        session_messages=[],
        budget=None,
        scent_text=res.text,
    )
    content = assembler._build_user(ctx)["content"]
    stored = content.split("\n\n", 1)[1]
    assert stored == "统计一下每个渠道的数据覆盖率"
    assert "UNTRUSTED" not in stored


def test_second_turn_followup_is_lean_against_real_history():
    history = [
        {"role": "user", "content": "这周的数据帮我整理个周报"},
        {"role": "assistant", "content": "好的,周报已生成..."},
    ]
    window = recent_user_window(history)
    assert not is_fat("再改短点吧这个周报", window)
    assert is_fat("帮我看看这段 python 报错是怎么回事", window)


def test_english_stopwords_strip_as_tokens_not_substrings():
    # "the" inside "theme" must survive token stripping.
    assert is_fat("fix the theme layout bug now", "")


# ---------------------------------------------------------------- find_skill


async def test_find_skill_formats_results():
    router = _Router([_Hit("local/sql-weekly-agg", "按周聚合的 SQL 模式")])
    out = await FindSkillTool(lambda: router).execute(query="weekly aggregation sql")
    assert "local/sql-weekly-agg" in out and "read_skill" in out


async def test_find_skill_handles_missing_query_router_and_errors():
    tool = FindSkillTool(lambda: None)
    assert "required" in await tool.execute(query="")
    assert "not available" in await tool.execute(query="x")
    out = await FindSkillTool(lambda: _Router(raise_=True)).execute(query="x")
    assert out.startswith("Error")
    out = await FindSkillTool(lambda: _Router([])).execute(query="x")
    assert "No matching skills" in out


def test_find_skill_description_matches_what_is_wired():
    with_hub = FindSkillTool(lambda: None, hub_wired=True)
    assert "16 categories" in with_hub.description
    assert "failed twice" in with_hub.description
    local_only = FindSkillTool(lambda: None)
    assert "110k" not in local_only.description
    assert "16 categories" not in local_only.description
    assert "locally installed" in local_only.description


async def test_find_skill_enforces_blocklist_and_safety_bar():
    hits = [_Hit("hub/blocked-one", "x"), _Hit("hub/low-safety", "y"), _Hit("hub/clean", "z")]
    hits[1].meta["score_safety"] = 0.1
    tool = FindSkillTool(lambda: _Router(hits), blocklist=["blocked-one"])
    out = await tool.execute(query="anything at all")
    assert "hub/blocked-one" not in out and "hub/low-safety" not in out
    assert "hub/clean" in out


# ---------------------------------------------------------------- read_skill


class _HubClient:
    def __init__(self, meta):
        self._meta = meta

    async def get(self, native):
        return self._meta


async def test_read_skill_refuses_blocklisted_ids():
    tool = ReadSkillTool(blocklist=["evil-skill"])
    out = await tool.execute(skill_id="hub/evil-skill")
    assert out.startswith("Error") and "blocklist" in out


async def test_read_skill_refuses_low_safety_hub_bodies():
    client = _HubClient({"slug": "shady", "name": "shady", "skill_md": "body", "score_safety": 0.1})
    tool = ReadSkillTool(client=client, min_safety=0.7)
    out = await tool.execute(skill_id="hub/shady")
    assert out.startswith("Error") and "score_safety" in out
    clean = _HubClient({"slug": "fine", "name": "fine", "skill_md": "body", "score_safety": 0.9})
    assert "body" in await ReadSkillTool(client=clean, min_safety=0.7).execute(skill_id="hub/fine")


# ---------------------------------------------------------------- nudge


def test_loop_break_nudge_mentions_find_skill_only_when_offered():
    plain = loop_break_nudge("exec", 3)
    assert "find_skill" not in plain
    hinted = loop_break_nudge("exec", 3, suggest_find_skill=True)
    assert "find_skill" in hinted
