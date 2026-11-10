"""The guard's audit trail: what it did, written where no model can read it.

The guard's commonest action leaves no trace by construction -- a benchmark row is
dropped and the model is shown a list, not a list with a hole. On the smoke run that
prompted this module, establishing the guard had fired at all took re-issuing the
model's own queries against the search vendor from outside the run. That is not a
check anyone repeats over a 2,400-item evaluation.
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

from research_flow.support import guard_audit  # noqa: E402
from research_flow.support.guard_audit import audit, audit_path, set_audit_dir  # noqa: E402
from research_flow.tools import web  # noqa: E402
from research_flow.tools.web import WebSearchTool, query_refusal, set_current_session  # noqa: E402

HF = "https://huggingface.co/datasets/org/copy"


@pytest.fixture(autouse=True)
def _audit_off_afterwards():
    yield
    set_audit_dir(None)


def rows(d: Path) -> list[dict]:
    p = d / "benchmark_guard.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class TestTheSwitch:
    def test_off_by_default_writes_nothing_and_raises_nothing(self, tmp_path):
        set_audit_dir(None)
        assert audit_path() is None
        audit("search_rows_dropped", dropped=3)
        assert not list(tmp_path.iterdir())

    def test_on_names_the_file_it_writes(self, tmp_path):
        set_audit_dir(tmp_path)
        assert audit_path() == str(tmp_path / "benchmark_guard.jsonl")

    def test_the_directory_is_created_on_first_row(self, tmp_path):
        d = tmp_path / "not" / "there" / "yet"
        set_audit_dir(d)
        audit("page_refused", url="https://example.org/x")
        assert rows(d)[0]["event"] == "page_refused"


class TestTheRows:
    def test_a_row_carries_the_event_its_fields_and_a_clock(self, tmp_path):
        set_audit_dir(tmp_path)
        audit("search_rows_dropped", query="browsecomp answers", dropped=5, organic=6)
        (r,) = rows(tmp_path)
        assert r["event"] == "search_rows_dropped"
        assert r["dropped"] == 5 and r["organic"] == 6 and r["query"] == "browsecomp answers"
        assert isinstance(r["ts"], float) and isinstance(r["pid"], int)

    def test_rows_append_rather_than_replace(self, tmp_path):
        set_audit_dir(tmp_path)
        for i in range(3):
            audit("fetch_refused", url=f"https://huggingface.co/datasets/x/{i}")
        assert [r["url"][-1] for r in rows(tmp_path)] == ["0", "1", "2"]

    def test_non_ascii_survives_rather_than_being_escaped(self, tmp_path):
        set_audit_dir(tmp_path)
        audit("query_refused", query="体育 huggingface 答案")
        assert rows(tmp_path)[0]["query"] == "体育 huggingface 答案"

    def test_a_row_stays_inside_one_buffered_write(self, tmp_path):
        """Several processes and sixteen agents append here. O_APPEND makes one write
        syscall atomic against another writer's; a row over the 8 KiB stdio buffer
        becomes more than one write and can be torn in half by a concurrent appender."""
        set_audit_dir(tmp_path)
        audit("search_rows_dropped", query="q" * 5000, urls=["https://x/" + "y" * 5000] * 50)
        line = (tmp_path / "benchmark_guard.jsonl").read_bytes()
        assert len(line) < 8192, len(line)
        r = rows(tmp_path)[0]
        assert len(r["urls"]) == 10 and all(len(u) <= 400 for u in r["urls"])


class TestItNeverBreaksTheToolCall:
    def test_an_unwritable_directory_is_swallowed(self, tmp_path, monkeypatch):
        # An audit failure must never turn a working fetch into an error: the trail is
        # for the person reading the run afterwards, not for the turn.
        set_audit_dir(tmp_path)
        monkeypatch.setattr(guard_audit, "open", _raising_open, raising=False)
        audit("page_refused", url="https://example.org/x")  # must not raise
        # And the patch bit: without it this row would be on disk, so a test that only
        # asserted "did not raise" would pass just as well with the patch inert.
        assert rows(tmp_path) == []

    def test_a_directory_that_cannot_be_made_is_swallowed(self, tmp_path, monkeypatch):
        d = tmp_path / "sub"
        set_audit_dir(d)
        monkeypatch.setattr(Path, "mkdir", _raising_mkdir)
        audit("page_refused", url="https://example.org/x")  # must not raise
        assert not d.exists()


def _raising_open(*a, **k):
    raise OSError("read-only file system")


def _raising_mkdir(self, *a, **k):
    raise OSError("read-only file system")


class _OrganicTransport(httpx.AsyncBaseTransport):
    """One SERP: two benchmark copies above one ordinary page."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "organic": [
                    {"title": "answer key", "link": HF + "/viewer/default/train"},
                    {"title": "another copy", "link": "https://hf.co/datasets/org/copy2"},
                    {"title": "Abbey Square", "link": "https://en.wikipedia.org/wiki/Abbey_Square"},
                ]
            },
        )


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


class TestTheSilentDropIsTheReasonThisExists:
    @pytest.mark.asyncio
    async def test_dropped_search_rows_are_counted_and_named(self, transport, tmp_path):
        transport(_OrganicTransport())
        set_audit_dir(tmp_path)
        set_current_session("audit-serp")
        tool = WebSearchTool(api_key="k", benchmark_guard=True, benchmark_guard_queries=False)

        rendered = await tool.execute(query="Abbey Square sculpture", count=3)

        assert "huggingface" not in rendered and "hf.co" not in rendered
        (r,) = rows(tmp_path)
        assert r["event"] == "search_rows_dropped"
        assert r["dropped"] == 2 and r["organic"] == 3
        assert any("huggingface.co" in u for u in r["urls"])

    @pytest.mark.asyncio
    async def test_a_serp_with_nothing_to_drop_writes_no_row(self, transport, tmp_path):
        """Or the trail says the guard fired on every search it ever saw."""
        transport(_OrganicTransport())
        set_audit_dir(tmp_path)
        set_current_session("audit-serp-clean")
        tool = WebSearchTool(api_key="k", benchmark_guard=False)

        await tool.execute(query="Abbey Square sculpture", count=3)

        assert rows(tmp_path) == []

    @pytest.mark.asyncio
    async def test_a_refused_query_is_recorded(self, transport, tmp_path):
        transport(_OrganicTransport())
        set_audit_dir(tmp_path)
        set_current_session("audit-query")
        tool = WebSearchTool(api_key="k", benchmark_guard=True)

        rendered = await tool.execute(query="huggingface browsecomp answer key", count=3)

        assert rendered == query_refusal()
        (r,) = rows(tmp_path)
        assert r["event"] == "query_refused" and "huggingface" in r["query"]
