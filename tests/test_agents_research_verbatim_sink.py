"""The verbatim body sink: what the tools returned, before three lossy stages ate it.

By the time a tool result reaches disk the digest has discarded most of the page,
the context trimmer has replaced whole bodies with a placeholder, and the ingest
cap has truncated what is left. So "what did this tool actually return" is
unanswerable after the fact. This sink closes that, and it is an instrument: off
unless an environment variable names a file, append-only, and a write failure is
logged rather than raised.

Every test below is written against the two ways an instrument fails: it changes
the thing it measures, or it stops writing and nothing notices.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.support import ledger as ledger_mod  # noqa: E402
from research_flow.tools.web import (  # noqa: E402
    DigestOutput,
    WebFetchTool,
    WebSearchTool,
    set_current_session,
)


def _patch_client(monkeypatch, transport):
    real = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("proxy", None)
        return real(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


class _PageTransport(httpx.AsyncBaseTransport):
    def __init__(self, text: str) -> None:
        self._text = text

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=self._text)


class _RefusingTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request, json={})


def _sink(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "bodies.jsonl"
    monkeypatch.setenv(ledger_mod.VERBATIM_ENV, str(path))
    ledger_mod._session_seq.pop(str(path), None)  # noqa: SLF001
    return path


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_off_by_default_writes_nothing_and_changes_nothing(monkeypatch, tmp_path):
    """The default path must stay byte-identical: every run already on disk was
    measured without this, and an instrument that moves the measurement is worse
    than no instrument."""
    monkeypatch.delenv(ledger_mod.VERBATIM_ENV, raising=False)
    _patch_client(monkeypatch, _PageTransport("page body"))
    set_current_session("t")

    out = await WebFetchTool(api_key="k").execute(url="https://a.example/one")

    payload = json.loads(out)
    assert payload["text"] == "page body"
    assert "_ledger_only" not in out
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_the_reader_branch_records_the_page_and_the_envelope(monkeypatch, tmp_path):
    """Two rows, not one. The delivered envelope is not a substitute for the source:
    a digested fetch delivers a small fraction of the page and the rest is written
    nowhere else."""
    path = _sink(monkeypatch, tmp_path)
    _patch_client(monkeypatch, _PageTransport("the whole page"))
    set_current_session("t")

    await WebFetchTool(api_key="k").execute(url="https://a.example/one")

    rows = _rows(path)
    phases = {r["phase"]: r for r in rows}
    assert set(phases) == {"source", "delivered"}
    assert phases["source"]["text"] == "the whole page"
    assert phases["source"]["url"] == "https://a.example/one"
    assert phases["source"]["chars"] == len("the whole page")
    assert json.loads(phases["delivered"]["text"])["text"] == "the whole page"
    assert all(r["op"] == "tool_body" and r["tool"] == "web_fetch" for r in rows)
    assert all(r["session_seq"] == 1 for r in rows)


@pytest.mark.asyncio
async def test_a_failed_fetch_records_no_body_but_still_ledgers(monkeypatch, tmp_path):
    """A refusal has an envelope but no page. The body sink must not invent a
    source row for text that was never served, and the ledger row that says the
    fetch failed still has to be written."""
    path = _sink(monkeypatch, tmp_path)
    ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(ledger_mod, "ledger_path", lambda: str(ledger_path))
    _patch_client(monkeypatch, _RefusingTransport())
    set_current_session("t")

    out = await WebFetchTool(api_key="k").execute(url="https://a.example/one")

    assert json.loads(out)["error"]
    assert [r["phase"] for r in _rows(path)] == ["delivered"]
    assert [r["op"] for r in _rows(ledger_path)] == ["fetch"]


@pytest.mark.asyncio
async def test_the_ledger_only_annotation_reaches_the_ledger_and_not_the_model(monkeypatch, tmp_path):
    """``DigestOutput.ledger`` is write-only. A digest that could label its own
    output in the tool result would be a digest the model can read the label of,
    so the annotation is stripped from the envelope and merged into the fetch row.
    """
    path = _sink(monkeypatch, tmp_path)
    ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(ledger_mod, "ledger_path", lambda: str(ledger_path))
    _patch_client(monkeypatch, _PageTransport("x" * 9000))
    set_current_session("t")

    async def _digest(text: str, want: str) -> DigestOutput:
        return DigestOutput(text="the distilled facts", ledger={"sidecar_mode": "orphan", "sidecar_items": 3})

    out = await WebFetchTool(api_key="k", digest_fn=_digest).execute(
        url="https://a.example/one", info_to_extract="the founding date"
    )

    assert "sidecar_mode" not in out and "_ledger_only" not in out
    assert json.loads(out)["text"] == "the distilled facts"
    delivered = next(r for r in _rows(path) if r["phase"] == "delivered")
    assert "sidecar_mode" not in delivered["text"]
    row = _rows(ledger_path)[0]
    assert row["sidecar_mode"] == "orphan" and row["sidecar_items"] == 3


@pytest.mark.asyncio
async def test_a_bare_string_digest_still_works(monkeypatch, tmp_path):
    """The annotation is optional. A digest that returns a plain string produces
    exactly the envelope it always did."""
    monkeypatch.delenv(ledger_mod.VERBATIM_ENV, raising=False)
    _patch_client(monkeypatch, _PageTransport("x" * 9000))
    set_current_session("t")

    async def _digest(text: str, want: str) -> str:
        return "the distilled facts"

    out = await WebFetchTool(api_key="k", digest_fn=_digest).execute(
        url="https://a.example/one", info_to_extract="the founding date"
    )

    assert json.loads(out)["text"] == "the distilled facts"
    assert "_ledger_only" not in out


@pytest.mark.asyncio
async def test_search_records_what_was_rendered(monkeypatch, tmp_path):
    """Written beside the ledger row rather than at the return, so a search that
    produced an error string is recorded as faithfully as one that found pages."""
    path = _sink(monkeypatch, tmp_path)
    set_current_session("t")
    tool = WebSearchTool(api_key="k")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: (_ for _ in ()).throw(RuntimeError("no network")))

    rendered = await tool.execute(query="who won?", count=3)

    rows = [r for r in _rows(path) if r["tool"] == "web_search"]
    assert len(rows) == 1
    assert rows[0]["phase"] == "delivered" and rows[0]["query"] == "who won?"
    assert rows[0]["text"] == rendered and rows[0]["chars"] == len(rendered)
    assert rows[0]["replay"] is False


@pytest.mark.asyncio
async def test_an_unwritable_sink_is_loud_and_harmless(monkeypatch, tmp_path):
    """An instrument that can kill the run it measures is worse than no instrument.
    The fetch still returns its page; the failure is logged."""
    monkeypatch.setenv(ledger_mod.VERBATIM_ENV, str(tmp_path / "no-such-dir" / "bodies.jsonl"))
    _patch_client(monkeypatch, _PageTransport("page body"))
    set_current_session("t")
    errors: list[str] = []
    monkeypatch.setattr(ledger_mod.logger, "error", lambda msg, *a, **kw: errors.append(msg))

    out = await WebFetchTool(api_key="k").execute(url="https://a.example/one")

    assert json.loads(out)["text"] == "page body"
    assert errors and "verbatim sink append failed" in errors[0]
