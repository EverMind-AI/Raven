"""The guard's content half: a page that reproduces the question is a copy of it.

Why there is a second rule at all, in one measurement. On a smoke run with the host
rule on, the model's three answer-seeking queries returned 21 organic rows, 17 of them
Hugging Face copies; every one was dropped and none reached the model. It scored anyway
by reading ``openevalz.com/trace/<id>``, a judge-prompt dump carrying the gold verbatim,
and opened a second copy through ``cdn-avatars.qwak.ai/datasets/...`` -- a Hugging Face
dataset page under a hostname no host list contains.

Over the same 400-item evaluation at a 60-character run, counting each rule only where
it applies -- this one on web_fetch alone -- leaks prevented go from 14/21 to 17/21, and
13 of the 243 clean correct items lose at least one fetched page (18 pages), so the cost is
between 8 and 13 items. Five of the 18 carried the gold, which does not make them leaks: two
are benchmark copies and three are one item's legitimate references (OEIS, arXiv, MathNet).
Carrying the answer is what a good source does; only reproducing the question makes a copy.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import FlowConfig  # noqa: E402
from research_flow.support import guard_audit  # noqa: E402
from research_flow.support.benchmark_guard import (  # noqa: E402
    QUESTION_RUN_CHARS,
    blocks_page,
    page_refusal,
)
from research_flow.tools import web  # noqa: E402
from research_flow.tools.web import (  # noqa: E402
    WebFetchTool,
    current_task,
    set_current_session,
    set_current_task,
)

# Long enough to clear the run length, and shaped like the benchmark questions this is for.
QUESTION = (
    "Which sculptor created the bronze statue of Seamus Rafter that stands on Abbey Square "
    "in Enniscorthy, County Wexford, and in which year was it unveiled?"
)
ZH_QUESTION = (
    "在某个知名的葡萄酒产区中的某个地区存在一个只产某种葡萄酒的地区，距离此地区20-40公里范围内"
    "存在一家足球俱乐部，那么该足球俱乐部最近一次升入上一级联赛提前几轮确定？"
)


class _PageTransport(httpx.AsyncBaseTransport):
    """Serves one fixed page body to whatever is asked for."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        return httpx.Response(200, request=request, text=self.body)


@pytest.fixture
def transport(monkeypatch):
    real = httpx.AsyncClient

    def install(t):
        def factory(**kwargs):
            kwargs.pop("proxy", None)
            kwargs.pop("transport", None)
            return real(transport=t, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        monkeypatch.setattr(web, "_RETRY_BACKOFF_S", tuple(0.001 for _ in web._RETRY_BACKOFF_S))
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port or 0))
            ],
        )
        return t

    return install


class TestBlocksPage:
    def test_a_page_quoting_the_question_is_a_copy_of_it(self):
        page = f"# HLE row 412\n\nprompt: {QUESTION}\n\nanswer: Michael Cullen, 2007\n"
        assert blocks_page(QUESTION, page)

    def test_a_page_about_the_same_subject_is_not(self):
        page = (
            "Enniscorthy is a town in County Wexford. Abbey Square holds several memorials, "
            "including one to the 1916 volunteer Seamus Rafter, cast in bronze."
        )
        assert not blocks_page(QUESTION, page)

    def test_a_truncated_quote_still_matches(self):
        # Every copy seen truncates the prompt somewhere: one ended mid-word with an
        # ellipsis. A first-window-only test would read that as no quote at all.
        page = "prompt: " + QUESTION[:95] + "…"
        assert blocks_page(QUESTION, page)

    def test_a_quote_reflowed_across_lines_still_matches(self):
        page = "prompt:\n  " + QUESTION.replace(" ", "\n  ") + "\nanswer: X"
        assert blocks_page(QUESTION, page)

    def test_a_cjk_question_is_reached(self):
        assert blocks_page(ZH_QUESTION, f"题目：{ZH_QUESTION}\n答案：1轮")
        assert not blocks_page(ZH_QUESTION, "这是一篇关于足球俱乐部升级的新闻报道。")

    def test_a_question_shorter_than_the_run_is_never_matched(self):
        # The deliberate hole: below the run length the rule cannot tell a copy from a
        # page sharing a short phrase, so it declines to guess. 44 of the 400 measured
        # questions fall here.
        short = "Who won the 1997 final?"
        assert len(short) < QUESTION_RUN_CHARS
        assert not blocks_page(short, f"{short} Answer: Bob")

    def test_the_run_length_is_a_knob_not_a_constant(self):
        page = "prompt: " + QUESTION[:70]
        assert blocks_page(QUESTION, page, run_chars=60)
        assert not blocks_page(QUESTION, page, run_chars=90)

    def test_empty_inputs_are_inert(self):
        assert not blocks_page("", "anything")
        assert not blocks_page(QUESTION, "")
        assert not blocks_page(QUESTION, QUESTION, run_chars=0)


class TestRefusalText:
    def test_it_says_why_rather_than_reading_as_a_fault(self):
        text = page_refusal()
        assert "Blocked:" in text and "error" not in text.lower()


class TestOffByDefault:
    def test_the_flow_config_default(self):
        g = FlowConfig().benchmark_guard
        assert g.enabled is False
        assert g.block_question_pages is True and g.question_run_chars == QUESTION_RUN_CHARS
        assert g.audit is True

    def test_the_sub_flags_are_inert_while_the_guard_is_off(self):
        tool = WebFetchTool(api_key="k")
        assert tool.benchmark_guard is False
        assert tool.benchmark_guard_question is True


class TestTheTaskContextVar:
    def test_it_defaults_empty_so_an_unset_turn_blocks_nothing(self):
        set_current_task("")
        assert current_task() == ""
        assert not blocks_page(current_task(), f"prompt: {QUESTION}")

    def test_it_round_trips(self):
        set_current_task(QUESTION)
        assert current_task() == QUESTION


class TestTheLivePath:
    """Through ``execute``, because the policy function passing says nothing about
    where the check sits -- and where it sits is the whole point: before the digest
    (the decisive page quoted the question only in its raw text) and before the page
    is recorded as a source (or it reaches the reviewer's evidence pack unseen)."""

    @pytest.mark.asyncio
    async def test_a_page_reproducing_the_question_is_refused(self, transport, tmp_path):
        t = transport(_PageTransport(f"prompt: {QUESTION}\n\ncorrect_answer: Michael Cullen"))
        set_current_session("page-guard")
        set_current_task(QUESTION)
        guard_audit.set_audit_dir(tmp_path)
        tool = WebFetchTool(api_key="k", benchmark_guard=True)

        out = await tool.execute(url="https://example.org/trace/1")

        assert out == page_refusal()
        assert t.calls, "the page is fetched, then declined -- the host was not the signal"
        rows = [json.loads(x) for x in (tmp_path / "benchmark_guard.jsonl").read_text().splitlines()]
        assert [r["event"] for r in rows] == ["page_refused"]
        assert rows[0]["rule"] == "question_verbatim"
        guard_audit.set_audit_dir(None)

    @pytest.mark.asyncio
    async def test_the_same_page_is_served_with_the_guard_off(self, transport):
        transport(_PageTransport(f"prompt: {QUESTION}\n\ncorrect_answer: Michael Cullen"))
        set_current_session("page-guard-off")
        set_current_task(QUESTION)
        tool = WebFetchTool(api_key="k")

        out = await tool.execute(url="https://example.org/trace/1")

        assert out != page_refusal(), "the control case must show what the guard removes"
        assert "Michael Cullen" in out

    @pytest.mark.asyncio
    async def test_an_ordinary_page_is_served_with_the_guard_on(self, transport):
        """Not vacuous: the guard on must not refuse everything."""
        transport(_PageTransport("Enniscorthy is a town in County Wexford, on the River Slaney. " * 20))
        set_current_session("page-guard-ok")
        set_current_task(QUESTION)
        tool = WebFetchTool(api_key="k", benchmark_guard=True)

        out = await tool.execute(url="https://en.wikipedia.org/wiki/Enniscorthy")

        assert out != page_refusal()
        assert "River Slaney" in out

    @pytest.mark.asyncio
    async def test_the_half_can_be_turned_off_alone(self, transport):
        """An evaluation comparing against the published host rule needs this."""
        transport(_PageTransport(f"prompt: {QUESTION}\n\ncorrect_answer: Michael Cullen"))
        set_current_session("page-guard-host-only")
        set_current_task(QUESTION)
        tool = WebFetchTool(api_key="k", benchmark_guard=True, benchmark_guard_question=False)

        out = await tool.execute(url="https://example.org/trace/1")

        assert out != page_refusal()

    @pytest.mark.asyncio
    async def test_the_refused_page_never_becomes_evidence(self, transport, monkeypatch):
        """The check sits before ``_record_source`` on purpose: a copy admitted to the
        source store reaches the reviewer's evidence pack even though the model never
        saw it, which would leak the answer one layer further in."""
        transport(_PageTransport(f"prompt: {QUESTION}\n\ncorrect_answer: Michael Cullen"))
        set_current_session("page-guard-source")
        set_current_task(QUESTION)
        recorded: list[str] = []
        tool = WebFetchTool(api_key="k", benchmark_guard=True)
        monkeypatch.setattr(type(tool), "_record_source", lambda self, url, text, ex: recorded.append(url))

        await tool.execute(url="https://example.org/trace/1")

        assert recorded == []

    @pytest.mark.asyncio
    async def test_a_turn_with_no_task_set_blocks_nothing(self, transport):
        """Fail-open is right here and only here: the rule's whole premise is a
        comparison against the question, and with no question there is nothing to
        compare. The host half still runs, which is what the guard had before."""
        transport(_PageTransport(f"prompt: {QUESTION}\n\ncorrect_answer: Michael Cullen"))
        set_current_session("page-guard-no-task")
        set_current_task("")
        tool = WebFetchTool(api_key="k", benchmark_guard=True)

        out = await tool.execute(url="https://example.org/trace/1")

        assert out != page_refusal()
