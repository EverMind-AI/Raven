"""End-to-end tests for SkillsSegmentBuilder.

The builder owns the rewriter → router → pre-gate hydrate → gate →
post-gate hydrate → render pipeline. Tests use stub sources / clients
to exercise each stage without network or LLM calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace as dataclass_replace
from pathlib import Path
from typing import Any

import pytest

from raven.context_engine.base import AssemblyContext, Segment, TokenBudget
from raven.context_engine.segments.skills import SkillsSegmentBuilder
from raven.memory_engine.skill_forge import (
    LLMGateFilter,
    QueryRewriter,
    SkillForgeRouter,
)
from raven.memory_engine.skill_forge.types import RouterHit


# ----------------------------------------------------------------------
# Stub doubles
# ----------------------------------------------------------------------


@dataclass
class _Resp:
    content: str
    finish_reason: str = "stop"


class _StubProvider:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def chat_with_retry(self, **_kwargs: Any) -> _Resp:
        if isinstance(self._response, _Resp):
            return self._response
        return _Resp(content=str(self._response))


class _StubSource:
    """SkillSource that returns a hard-coded hit list."""

    def __init__(self, name: str, hits: list[RouterHit], weight: float = 1.0) -> None:
        self.name = name
        self.weight = weight
        self._hits = hits

    async def search(
        self, query: str, history: list[dict[str, Any]], k: int,
    ) -> list[RouterHit]:
        return list(self._hits[:k])


class _StubHubClient:
    """Records get / install calls and returns canned payloads."""

    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        self._payloads = payloads
        self.get_calls: list[str] = []
        self.install_calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(self, skill_id: str) -> dict[str, Any]:
        self.get_calls.append(skill_id)
        return dict(self._payloads[skill_id])

    async def install(
        self, skill_id: str, *, prefetched_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.install_calls.append((skill_id, prefetched_meta))
        payload = dict(self._payloads[skill_id])
        return {
            "slug": payload.get("slug", skill_id),
            "version": payload.get("version", "v0"),
            "dir": payload.get("_dir", "/tmp/" + skill_id),
            "scripts_dir": None,
            "skill_md": payload.get("skill_md", ""),
        }


def _hit(qid: str, name: str, body: str = "", **meta: Any) -> RouterHit:
    source = qid.split("/", 1)[0]
    meta.setdefault("source", source)
    if source == "hub":
        meta.setdefault("id", qid.split("/", 1)[1])
    return RouterHit(
        qualified_id=qid, name=name, content=body, score=0.5, meta=meta,
    )


def _ctx(message: str) -> AssemblyContext:
    return AssemblyContext(
        session_key="s",
        current_message=message,
        media=None,
        channel=None,
        chat_id=None,
        session_messages=[],
        budget=TokenBudget(
            context_length=200_000, reserved_output=8000,
            reserved_tools=4000, reserved_system=4000,
            available_history=184_000,
        ),
    )


# ----------------------------------------------------------------------
# Baseline — no rewriter, no gate, no hub
# ----------------------------------------------------------------------


async def test_baseline_renders_local_hits() -> None:
    src = _StubSource("local", [
        _hit("local/foo", "foo", body="body foo"),
        _hit("local/bar", "bar", body="body bar"),
    ])
    router = SkillForgeRouter([src])
    builder = SkillsSegmentBuilder(router, skill_top_k=2)
    seg = await builder.build(_ctx("anything"))
    assert seg is not None
    assert "# Skills" in seg.text
    assert "foo" in seg.text and "bar" in seg.text
    assert seg.meta["injected_skill_ids"] == ["local/foo", "local/bar"]


async def test_no_router_returns_empty_segment() -> None:
    builder = SkillsSegmentBuilder(None)
    seg = await builder.build(_ctx("q"))
    assert seg.text == ""
    assert seg.meta["injected_skill_ids"] == []


# ----------------------------------------------------------------------
# Rewriter stage
# ----------------------------------------------------------------------


async def test_rewriter_skip_short_circuits_segment() -> None:
    src = _StubSource("local", [_hit("local/foo", "foo", body="b")])
    router = SkillForgeRouter([src])
    rewriter = QueryRewriter(_StubProvider(
        json.dumps({"need_retrieval": False})
    ))
    builder = SkillsSegmentBuilder(router, rewriter=rewriter)
    seg = await builder.build(_ctx("hello there"))
    assert seg.text == ""
    assert seg.meta.get("rewriter_skipped") is True
    assert seg.meta["injected_skill_ids"] == []


async def test_rewriter_rewrite_passes_through() -> None:
    """When rewriter returns a rewritten_query, the router should be
    invoked with it (not the original)."""
    received: list[str] = []

    class _SpySource:
        name = "local"
        weight = 1.0

        async def search(self, query, history, k):  # noqa: D401
            received.append(query)
            return []

    router = SkillForgeRouter([_SpySource()])
    rewriter = QueryRewriter(_StubProvider(json.dumps({
        "need_retrieval": True, "rewritten_query": "pdf gen",
    })))
    builder = SkillsSegmentBuilder(router, rewriter=rewriter)
    await builder.build(_ctx("please generate me a pdf report"))
    assert received == ["pdf gen"]


# ----------------------------------------------------------------------
# Pre-gate body hydrate (Hub)
# ----------------------------------------------------------------------


async def test_pre_gate_hydrate_fills_hub_body() -> None:
    hub_hit = _hit("hub/abc", "Calendar", body="")
    src = _StubSource("hub", [hub_hit])
    hub_client = _StubHubClient({
        "abc": {"name": "Calendar", "skill_md": "# Hub body content",
                "slug": "calendar", "version": "1.0"},
    })
    router = SkillForgeRouter([src])
    builder = SkillsSegmentBuilder(router, skill_top_k=1, hub_client=hub_client)
    seg = await builder.build(_ctx("schedule"))
    # The hub body should appear post-hydrate.
    assert "Hub body content" in seg.text
    assert hub_client.get_calls == ["abc"]


async def test_pre_gate_skipped_when_no_hub_client() -> None:
    """Hub hit with empty content + no client → content stays empty,
    render header still appears, body is just blank under the heading."""
    src = _StubSource("hub", [_hit("hub/abc", "Calendar", body="")])
    builder = SkillsSegmentBuilder(SkillForgeRouter([src]), skill_top_k=1)
    seg = await builder.build(_ctx("schedule"))
    assert "Calendar" in seg.text
    # No prefetch attempted, no body, no crash.


# ----------------------------------------------------------------------
# Gate stage
# ----------------------------------------------------------------------


async def test_gate_filters_pool_down_to_selected() -> None:
    src = _StubSource("local", [
        _hit("local/keep", "keep", body="k"),
        _hit("local/drop", "drop", body="d"),
    ])
    gate = LLMGateFilter(
        _StubProvider(json.dumps({"plan": "p", "skills": ["local/keep"]})),
        max_select=2,
    )
    builder = SkillsSegmentBuilder(
        SkillForgeRouter([src]), gate=gate, gate_pool_size=5,
    )
    seg = await builder.build(_ctx("task"))
    assert seg.meta["injected_skill_ids"] == ["local/keep"]
    assert "drop" not in seg.text


async def test_gate_empty_selection_yields_empty_segment() -> None:
    src = _StubSource("local", [_hit("local/foo", "foo", body="x")])
    gate = LLMGateFilter(
        _StubProvider(json.dumps({"plan": "none fits", "skills": []})),
    )
    builder = SkillsSegmentBuilder(SkillForgeRouter([src]), gate=gate)
    seg = await builder.build(_ctx("task"))
    assert seg.text == ""
    assert seg.meta["injected_skill_ids"] == []


# ----------------------------------------------------------------------
# Post-gate refs hydrate
# ----------------------------------------------------------------------


async def test_post_gate_resolves_local_refs(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "references" / "x.md").write_text("ref body")

    body = "Read {baseDir}/references/x.md."
    src = _StubSource("local", [_hit(
        "local/foo", "foo", body=body, skill_dir=str(skill_dir),
    )])
    builder = SkillsSegmentBuilder(SkillForgeRouter([src]), skill_top_k=1)
    seg = await builder.build(_ctx("anything"))
    assert f"{skill_dir}/references/x.md" in seg.text
    assert "{baseDir}" not in seg.text


async def test_post_gate_install_hub_passes_prefetched_meta(tmp_path: Path) -> None:
    """install() should receive prefetched_meta from the pre-gate get()
    call — that's how we skip the redundant HTTP round-trip."""
    hub_hit = _hit("hub/x1", "Hub Skill", body="")
    src = _StubSource("hub", [hub_hit])
    skill_dir = tmp_path / "x1"
    skill_dir.mkdir()
    hub_client = _StubHubClient({
        "x1": {"name": "Hub Skill", "skill_md": "# Body",
               "slug": "x1", "version": "1.0", "_dir": str(skill_dir)},
    })
    builder = SkillsSegmentBuilder(
        SkillForgeRouter([src]), skill_top_k=1, hub_client=hub_client,
    )
    await builder.build(_ctx("q"))
    assert hub_client.get_calls == ["x1"]
    assert len(hub_client.install_calls) == 1
    sid, prefetched = hub_client.install_calls[0]
    assert sid == "x1"
    assert prefetched is not None
    assert prefetched.get("skill_md") == "# Body"


# ----------------------------------------------------------------------
# Tool-names collection
# ----------------------------------------------------------------------


async def test_get_tool_names_extracts_from_openai_schema() -> None:
    captured: list[list[str] | None] = []

    class _CaptureGate(LLMGateFilter):  # type: ignore[misc]
        async def filter(self, task, candidates, available_tools=None):  # type: ignore[override]
            captured.append(available_tools)
            return []

    gate = _CaptureGate(_StubProvider(json.dumps({"plan": "", "skills": []})))
    src = _StubSource("local", [_hit("local/a", "a")])

    def tool_defs() -> list[dict]:
        return [
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "exec"}},
            {"name": "flat_form"},
        ]

    builder = SkillsSegmentBuilder(
        SkillForgeRouter([src]), gate=gate, get_tool_definitions=tool_defs,
    )
    await builder.build(_ctx("task"))
    assert captured == [["read_file", "exec", "flat_form"]]
